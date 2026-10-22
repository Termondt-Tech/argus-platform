"""The read-only monitoring surface — live status, not a screener (spec §3, §5).

Spec §5 is explicit that this app's UI is "live status + kill switch, not a
screener" — the Dash/AG-Grid pattern is the wrong tool here. This module is the
data side of that: a single point-in-time picture of what the always-on process
is doing right now, assembled from in-process state plus a couple of small
read-only DB queries, and rendered two ways from ONE object:

- `GET /status` serializes the `StatusSnapshot` to JSON (machine-readable).
- `GET /` renders the SAME snapshot as plain, dependency-free HTML, legible on a
  phone browser.

Both are served by the existing `control.py` server, behind the same
Tailscale-only bind and the same `ARGUS_KILL_TOKEN` bearer auth as `POST /kill`
(§6). Nothing here changes system state.

Three pieces:

- `SessionStatus` — a tiny, process-lifetime, mutable publisher of what the
  CURRENT trading-day session is doing. The session body (`session.run_session`)
  lives and dies with the trading day and its live objects (`md`, the account
  stream, the risk-snapshot builder) are session-local; the control server is
  process-lifetime and can't reach them. So the session PUBLISHES its phase and a
  handle to its live risk snapshot here, and clears them when it ends. This is the
  seam that lets `/status` report freshness ages and session phase without
  reaching into a session it doesn't own.

- `StatusSnapshot` — the immutable assembled picture. Data-gathering happens once
  (`StatusService.assemble`); the JSON and HTML renderings are pure functions of
  this one object, so the two views can never drift.

- `StatusService.assemble` — the single data-gathering function. It reads
  process-lifetime collaborators (kill switch, heartbeat, audit log) and the LIVE
  daily-loss breaker directly, and issues ONE off-loop, read-only DB round trip
  through the writer's own connection (`AuditLog.read`) for the recent audit tail,
  today's raw `shadow.would_submit` count, and the last kill-switch trip.

  **A live rail is read from the rail, never inferred from a record.** A row
  outlives the process that wrote it; a rail's state does not. That distinction has
  now been got wrong twice here — once for the kill switch's `source`/`since`, once
  for the breaker (see `StatusSnapshot.breaker_tripped`) — and both times the page
  rendered a reassuring halt that was not actually in force. Counts and history come
  from the DB; current state comes from the object.

**No secret can surface here.** The bearer token lives only in the control
server and the environment; it is never written to the audit log, so it cannot
appear in the recent-record tail, and no snapshot field carries it. Audit
payloads are secret-free by existing design (OAuth secrets never enter a record —
see CLAUDE.md's DEBUG-logging note), so the tail is safe to surface over the
authenticated, private-interface endpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import html
import json
import logging
import math
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

from argus._version import UNKNOWN, build_version, is_dirty
from argus.audit.log import AuditLog
from argus.clock import et_day_bounds_utc_iso, et_now
from argus.heartbeat import LoopHeartbeat
from argus.risk.breaker import DailyLossBreaker
from argus.risk.killswitch import KillSwitch
from argus.risk.loss_budget import LossBudget
from argus.risk.models import RiskSnapshot
from argus.staleness import is_stale

logger = logging.getLogger("argus.status")

# Audit categories the status view reads back. Kept as constants so a rename of
# the emitting call site is a single, greppable coupling rather than a silent
# string mismatch that would zero the count.
#
# `daily_loss.breach` is deliberately NOT among them any more. This module read it
# until 2026-08-20 to answer "is the breaker tripped?", which is a question about a
# live object that a row cannot answer. → `StatusSnapshot.breaker_tripped`.
_WOULD_SUBMIT = "shadow.would_submit"
_KILL_TRIP = "kill_switch.trip"


class SessionPhase(str, Enum):
    """What the trading-day session is doing, coarse enough to glance at.

    A `str` enum so the value serializes to JSON as-is and reads plainly in HTML.
    """

    IDLE = "idle"                # process up, no session active (nights/weekends/pre-open)
    LOGGING_IN = "logging_in"    # session started; opening broker sessions (may be retrying)
    RECONCILING = "reconciling"  # logged in; reconciling broker vs. expected positions
    RUNNING = "running"          # signal loop live
    ENDED = "ended"              # today's session completed normally (reached the close/stop)
    ABORTED = "aborted"          # today's session gave up early (login/reconcile/account)


#: Where resident set size is read from. Linux only, by design — the deployment is
#: a Linux container and this must never be a dependency (no psutil in a
#: `--require-hashes` image built for a minimal auditable supply chain).
STATM_PATH = "/proc/self/statm"


def read_rss_bytes(statm_path: str = STATM_PATH) -> int | None:
    """Resident set size in bytes, or None where it cannot be read.

    `/proc/self/statm` is seven page counts; the SECOND is resident. One tiny file
    read, no allocation to speak of, safe to do on the event loop.

    Returns None rather than raising on any failure — a missing `/proc` (macOS
    dev machines), a permission problem, an unparseable line. This is a
    DIAGNOSTIC field on a read-only endpoint: it must never be able to take down
    the status page, let alone the process.
    """
    try:
        with open(statm_path) as fh:
            resident_pages = int(fh.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def gc_snapshot() -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """`(counts, cumulative_collections)` per GC generation.

    Both are O(1) reads of numbers CPython already maintains. **Deliberately NOT
    `len(gc.get_objects())`**, which materializes a list of every tracked object in
    the process — that would be a multi-hundred-millisecond event-loop stall
    inside the very endpoint added to diagnose event-loop stalls.

    `collections` is CUMULATIVE since process start. **Never divide it by
    `uptime_s`**: the import graph and first-session warmup trigger gen-2 sweeps
    before the first `/status` is served, and that fixed cost reads as a rate for
    hours. The per-hour figure is a trailing slope (`TrailingRates`); comparing it
    against the observed lag ramp is the measurement this field set exists for.
    """
    counts = gc.get_count()
    stats = gc.get_stats()
    collections = tuple(int(s.get("collections", 0)) for s in stats[:3])
    # CPython always has three generations, but do not let an implementation
    # detail turn a diagnostic into an IndexError.
    collections = collections + (0,) * (3 - len(collections))
    return (counts[0], counts[1], counts[2]), collections[:3]


#: The window the per-hour RSS and gen-2 rates are measured over, and the sampling
#: cadence that feeds it. Module constants rather than config keys, the same category
#: as `recent_limit`: they bound a DIAGNOSTIC, not behaviour, and a new REQUIRED key
#: would force a coupled deploy for a monitoring field.
RATE_WINDOW_S = 3600.0
RATE_SAMPLE_INTERVAL_S = 60.0
#: After the first failed sample (logged with its traceback), log every Nth — an
#: hour of a persistent failure at the default cadence.
_SAMPLER_LOG_EVERY = 60


@dataclass(frozen=True)
class _RateSample:
    #: Seconds on a MONOTONIC clock — never the wall clock (see `TrailingRates`).
    at: float
    rss_bytes: int | None
    gen2: int


@dataclass(frozen=True)
class TrailingRate:
    """RSS growth and gen-2 collections per hour over the trailing window."""

    rss_bytes_per_hour: float | None
    gen2_per_hour: float
    #: The span actually measured over, in [window, window + one sample interval).
    window_s: float


class TrailingRates:
    """A trailing-window slope for the two cumulative process figures (2026-09-01).

    **Why not `cumulative / uptime`.** Both figures carry a fixed STARTUP cost —
    ~5 MB of RSS and a gen-2 sweep or two before the first `/status` is served — and
    dividing a fixed amount by uptime yields a bias `startup / uptime` that decays and
    is never removed. It read 43 MB/h and 16 gen2/h at uptime 447 s on a process whose
    RSS and gen-2 count had not moved since 81 s; the sustained figures were 1.5 MB/h
    and 0.039/h. No uptime floor fixes that, because the bias is continuous.

    A slope over the last hour has no such term: the startup cost is inside the
    window only while startup is inside the hour, and is gone entirely once it is
    not. **Nothing is reported until one full window has been observed**, so the
    first hour of any process reads `None` — deliberately, because the one honest
    answer in that hour is "not yet measurable", and `rss_growth_bytes` still gives
    the absolute figure from the first poll.

    Pure and clock-free: the caller supplies every timestamp, as SECONDS ON A
    MONOTONIC CLOCK (`StatusService` passes `time.monotonic()`). Never the wall
    clock: an NTP step forward would stretch or shrink a window's span and skew one
    hour's slope, and a step backward would stall sampling until the clock caught
    up. A span is a duration, and a duration is what the monotonic clock measures.
    Bounded at roughly `window / sample interval + 2` samples however long the
    process lives.
    """

    def __init__(self, *, window_s: float = RATE_WINDOW_S) -> None:
        self._window_s = window_s
        self._samples: list[_RateSample] = []

    def add(self, at: float, rss_bytes: int | None, gen2: int) -> None:
        # A sample that does not move time forward is dropped rather than allowed to
        # produce a zero or negative span. A monotonic clock cannot step backwards;
        # this still guards a caller that feeds the same instant twice.
        if self._samples and at <= self._samples[-1].at:
            return
        self._samples.append(_RateSample(at, rss_bytes, gen2))
        # Keep exactly ONE sample at least a window old — the anchor a query needs —
        # and drop everything older than it.
        while (
            len(self._samples) >= 2
            and at - self._samples[1].at >= self._window_s
        ):
            del self._samples[0]

    def rate(self, at: float, rss_bytes: int | None, gen2: int) -> TrailingRate | None:
        """The slope from the newest sample at least a window before `at` to the
        reading at `at`, or None if no sample is that old yet."""
        anchor = None
        for s in self._samples:
            if at - s.at >= self._window_s:
                anchor = s
            else:
                break
        if anchor is None:
            return None
        span_s = at - anchor.at
        hours = span_s / 3600.0
        rss_rate = (
            None
            if rss_bytes is None or anchor.rss_bytes is None
            else (rss_bytes - anchor.rss_bytes) / hours
        )
        return TrailingRate(
            rss_bytes_per_hour=rss_rate,
            gen2_per_hour=(gen2 - anchor.gen2) / hours,
            window_s=span_s,
        )


@dataclass(frozen=True)
class LinkHealth:
    """Connection health for the two streaming links, as of right now.

    Added 2026-08-05. `/status` could report how OLD the data was
    (`market_data_age_s`, `account_data_age_s`) but nothing about the links
    carrying it, so the only way to ask "is my account link healthy?" was the
    session's journal line — whose `{...}` block is an ALL-TIME ledger and was
    misread as an active fault three times in one day. An operator on a phone at
    10:42 with a position open cannot difference two journal lines; this is the
    field set that answers the question directly.

    Deliberately DERIVED, not stored: every number here is read live off the
    adapters (`DXLinkMarketData.connected`/`.reconnects`,
    `AlertStreamerAccount.connected`/`.reconnects`/
    `.consecutive_refresh_failures`), so nothing new is measured and nothing can
    drift from what the adapters actually believe. It lives in `status.py` and is
    built BY the session, which keeps this module free of any marketdata/account
    import.

    All counts are SESSION-scoped, because the adapters are: they are rebuilt each
    trading day, so `reconnects` is "this session", never a career total. That is
    the opposite of the journal block and is the whole point.
    """

    market_data_connected: bool
    market_data_reconnects: int
    account_connected: bool
    account_reconnects: int
    #: Consecutive failures of the REST liveness re-seed, right now. 0 means the
    #: last attempt succeeded. This is the leading indicator for
    #: `account_data_age_s` climbing toward `account_stream_max_age_s` and
    #: blocking entries — it moves first.
    account_refresh_consecutive_failures: int
    #: Buffered-but-unconsumed events per type, read live off each SDK streamer.
    #: The SDK builds every one of these with
    #: `create_memory_object_stream(math.inf)` — UNBOUNDED — so a depth climbing
    #: through the session is an accumulator, and depths near zero exonerate the
    #: streaming path. `{}` when the streamer is down or its internals moved.
    market_data_queue_depths: dict[str, int] = field(default_factory=dict)
    account_queue_depths: dict[str, int] = field(default_factory=dict)


#: How much runway the macro-event blackout list must have before `/status` says
#: anything about it. Thirty days, and the number is a MODULE CONSTANT rather than a
#: config key on purpose: it bounds a WARNING, not behaviour, so it is the same
#: category as `RATE_WINDOW_S` and `recent_limit` rather than
#: the category `[staleness]` and `[coa]` are in — and a new REQUIRED key would force
#: the config to deploy with the image for a monitoring nicety.
#:
#: Thirty is sized to the WORK, which is what makes it the right order of magnitude:
#: regenerating the list needs FRED to have published next year's CPI/NFP/PCE release
#: dates and needs the FOMC calendar hand-transcribed into `fomc_dates.txt`, then a
#: config-only deploy. A month is enough to notice, chase a source that may not have
#: published yet, and ship — and short enough that the warning still means "act".
#: There is no in-repo precedent for a threshold in DAYS; every existing one is in
#: seconds and bounds a live rail.
BLACKOUT_WARN_DAYS = 30


@dataclass(frozen=True)
class BlackoutRunway:
    """How much life is left in the macro-event entry blackout list.

    `blackout_dates.txt` is a FINITE, generated file — `python -m argus.tools.blackout_dates`
    writes a fixed horizon (5 months, as generated 2026-08-04) — and the day it runs
    out, Argus starts trading FOMC, CPI, NFP and PCE days exactly as if they were
    ordinary. **From inside the process that is indistinguishable from a genuinely
    quiet calendar**: the blackout's only observable is entries NOT happening, so an
    exhausted list and a month with no macro events produce identical behaviour and
    identical logs. Nothing fires. The 2026-08-04 decision was to rely on a calendar
    reminder; this is the field that replaces it.

    Same construction rule as `LinkHealth` and the watchdog rows: DERIVED, never
    stored, and built BY the caller (`Application.startup`, which holds the config)
    so this module keeps its one-way dependency and imports no config loader.

    **Read once, at process start.** The runtime re-reads the file at every session
    start, so a regenerated list takes effect for TRADING at the next session — but
    this field will keep reporting the old horizon until the process restarts. That is
    the documented deploy anyway (a config-only release is checkout plus
    `systemctl restart`), and the failure direction is a warning that outstays its
    cause rather than one that never appears.

    The state is derived against `StatusSnapshot.generated_at`, never captured here,
    so the countdown ticks down on a process that has been up for weeks — the same
    live-derivation rule `next_session_open` follows.

    **It also answers "is TODAY a blackout day?"** (2026-09-18, `covers`). Until then
    it carried only the file's horizon, so on the one day a month the list was doing
    its job the row read a plain `ok` and nothing on the page said entries would not
    happen — while `next_session_open` announced a session that would open, reconcile,
    watch, and take no entries. `covers` is derived live against the snapshot's clock,
    like `state`. **The read-once caveat above is sharper for this claim than for the
    horizon**: a statement about today can be wrong, where a horizon can only be
    stale. It is still the deploy's rule that makes it hold — `blackout_dates.txt` is
    a bind mount, so the deploy classifier restarts on any change to it, and there is
    no hot-reload — and the SESSION's own verdict, from the set the strategy actually
    holds, is written at session start (`session.event_blackout`), so a hand edit
    without a restart is caught by the journal and the audit trail if not the page.
    """

    #: `[events] enabled`. False (or no `[events]` section at all) means the blackout
    #: is not in force and there is no runway to report — a real state, and the only
    #: place it is visible.
    enabled: bool
    #: The LATEST date in the list. None when the list is empty or could not be read;
    #: `date_count` and `error` are what tell those two apart.
    last_date: date | None = None
    #: How many dates the file holds. None means it could not be read at all.
    date_count: int | None = None
    #: Why the file could not be read, if it could not. A `ConfigError` here also
    #: means every SESSION will abort at start, so this is a loud state.
    error: str | None = None
    warn_days: int = BLACKOUT_WARN_DAYS
    #: Every date in the list — the set `CreditSpreadStrategy` is built from, read by
    #: the same loader. Empty when disabled or unreadable; `covers` tells those apart.
    #: Out of the repr: it is the one field too long to be useful in a log line.
    dates: frozenset[date] = field(default=frozenset(), repr=False)

    def covers(self, day: date) -> bool | None:
        """Whether `day` is a blackout day — no new entries that trading day.

        `False` when the blackout is disabled: that is a definite answer (nothing is
        suppressed), not an unknown one. `None` when the file could not be read,
        because then the page genuinely cannot say — and the session that loads the
        same file will abort at start anyway (see `state`'s `unreadable`).
        """
        if not self.enabled:
            return False
        if self.error is not None or self.date_count is None:
            return None
        return day in self.dates

    def days_remaining(self, today: date) -> int | None:
        """Days from `today` to the last date in the list; negative once past."""
        if self.last_date is None:
            return None
        return (self.last_date - today).days

    def state(self, today: date) -> str:
        """One word for the row, and the only place the thresholds are applied.

        `unreadable` and `exhausted` are deliberately separate from `expiring`: the
        first two mean the blackout is covering NOTHING right now, which is a
        different sentence from "it will stop covering things next month".
        """
        if not self.enabled:
            return "disabled"
        if self.error is not None or self.date_count is None:
            return "unreadable"
        # An empty-but-valid file is `exhausted`, not `ok`. `load_blackout_dates`
        # accepts a file of pure comments, and the generator's own refusal to write
        # one covers generation — not a file that was hand-trimmed afterwards.
        remaining = self.days_remaining(today)
        if remaining is None or remaining < 0:
            return "exhausted"
        return "expiring" if remaining <= self.warn_days else "ok"


@dataclass(frozen=True)
class PositionEvaluability:
    """One open position whose exit triggers are not all evaluable, as of right now.

    Added 2026-08-13, and for the same reason as `LinkHealth`: `/status` could report
    that positions were open but nothing about whether the watchdog could actually
    evaluate them. The audit trail cannot cover this on a phone — `watchdog.check`
    carries `unknown_reasons` on every tick, but audit payloads serialize with sorted
    keys, which puts `unknown_reasons` last, and the HTML tail truncates at 240
    characters, so a degraded position's reasons are ALWAYS cut. Reading it live off
    the watchdog sidesteps the truncation instead of working around it.

    Same construction rule as `LinkHealth`: DERIVED, never stored, and built BY the
    session from `PositionWatchdog.impaired_positions()` — which in turn reads the
    very `TriggerEvaluation` that produced the audit record, so the phone view and
    the audit trail cannot disagree. Defined here rather than imported from
    `execution/` so this module keeps its one-way dependency.

    `blind` is the severity that pages; a position with `blind=False` still has its
    underlying backstop live and is NOT an emergency (see the watchdog's
    `_report_evaluability`). It measures the MARKET triggers only — the time-based
    flatten has no input that can go missing, and counting it would make `blind`
    unreachable. `impaired_for_s` is what distinguishes a two-second quote gap from
    a condition that has not cleared all morning.
    """

    external_identifier: str
    candidate_id: str
    unknown_reasons: tuple[str, ...]
    blind: bool
    impaired_for_s: float


@dataclass(frozen=True)
class UnclosablePosition:
    """One open position whose ONE exit attempt is spent, as of right now.

    Added 2026-08-21 (A2b), and it is `PositionEvaluability`'s successor state, not
    a variant of it. A position whose exit attempt has been spent is popped out of
    the watchdog's `_impaired` map on the way past — correctly, since it is no
    longer being evaluated and a frozen impairment reading would be a lie that only
    gets older — and until this field existed it then appeared nowhere else. The
    page rendered the worst state the exit path can produce as an ordinary open
    position. Same shape as the `daily_loss.breach` defect one field over: a status
    view derived from a structure the real state has already left, reassuring at
    exactly the moment it is wrong.

    Same construction rule as `PositionEvaluability`: DERIVED, never stored, built
    BY the session from `PositionWatchdog.spent_exit_attempts()` — a live read
    scoped to what the ledger still says is held, so the row clears when the
    position does. Defined here rather than imported from `execution/` so this
    module keeps its one-way dependency.

    Every entry is an emergency; there is no `blind`-style severity split. The
    stop-loss condition already fired, nothing automated will try again, and it is
    0DTE. `outcome` is what tells the operator which kind of manual intervention this
    is, and `spent_for_s` is measured from the attempt itself, so it never restarts.

    **The mark arrived 2026-08-28**, with the change that made the watchdog keep
    PRICING a position whose send is spent. Before it, this row said a position could
    not be closed and could not say what it was worth — the half a human actually
    needs to decide whether to leg out by hand, and the half that was missing for 76
    minutes on the day the split was written. `None` means the last tick could not
    price it, and is rendered as such rather than as a number.
    """

    external_identifier: str
    candidate_id: str
    contracts: int
    outcome: str
    spent_for_s: float
    cost_to_close: float | None = None
    credit_stop: float | None = None
    still_triggered: bool | None = None
    #: The exception that spent the attempt, when a crash is what spent it
    #: (2026-09-10). `reject_reason`/`mutation_error` name the BROKER's account of a
    #: spent attempt; this names Argus's own, and their shared rule — nothing decides
    #: on these fields, they are the operator's diagnosis — is why it is a third
    #: sentence rather than an overload of either.
    crash_error: str | None = None
    #: The stub-close allowance's state (2026-09-24): `available` / `sent` /
    #: `exhausted` / `disabled`, or None where the row is not a stub (an
    #: outcome other than `filled`, or an unknown origin — no continuation
    #: exists there to describe). The page's "NO EXIT LEFT" headline predates
    #: the continuation, so this field is what separates "Argus still counts
    #: itself as holding one further close" from "the page is the only thing
    #: left besides the 15:30 flatten".
    stub_close: str | None = None
    #: A CLOSING order still working at the broker for this position (2026-09-25) —
    #: the crossing limit a `crossed` exit left resting. On 2026-09-24 the page read
    #: "no exit left" for 22 minutes while one was; with these set it says so, and
    #: says to check the broker before sending anything by hand. All None otherwise.
    working_close_order_id: int | None = None
    working_close_limit: float | None = None
    working_close_for_s: float | None = None
    #: `watchdog.credit_trigger_enabled` (2026-09-27). False: `credit_stop` is None on
    #: purpose, and the row reads "credit trigger off" — never "vs stop ?", which would
    #: present a switched-off level (or a missing one) as the stop. REQUIRED.
    credit_trigger_enabled: bool = field(kw_only=True)


@dataclass(frozen=True)
class WatchdogHealth:
    """The watchdog's own health, as of right now. The status-layer twin of the
    execution layer's `WatchdogHealth` (2026-09-10).

    Same construction rule as `PositionEvaluability` and `UnclosablePosition`:
    DERIVED, never stored, built BY the session from
    `PositionWatchdog.watchdog_health()`, and defined here rather than imported from
    `execution/` so this module keeps its one-way dependency.

    Three states, and the third is why the row exists at all. A watchdog that parks
    the process is loud; a watchdog that keeps running while failing every tick is,
    by construction, the quietest failure this system can produce — `phase: running`,
    sessions scheduled, the 15-second status line printing. `failures > 0` with
    `degraded=False` is the RECOVERED state: watching again, entries still halted,
    because the halt is permanent and the failure is not.
    """

    degraded: bool
    #: Spreads the ledger currently says are open — the count behind "N position(s)".
    watching: int
    #: Process-lifetime total failures, recovered or not.
    failures: int
    where: str | None
    error_type: str | None
    error: str | None
    #: None unless currently degraded.
    degraded_for_s: float | None
    #: Age of the most recent failure. None if there has never been one.
    last_failure_for_s: float | None
    #: Age of the last pass that completed with no failure. None until one has.
    last_clean_pass_s: float | None
    #: Identifiers that failed in THIS episode. Empty once recovered.
    affected: tuple[str, ...]


@dataclass(frozen=True)
class RetiredProfitTargetRow:
    """One HELD position whose profit target will send nothing further. (2026-09-23)

    The status-layer twin of the watchdog's `RetiredProfitTarget`, under the same
    construction rule as every row here: DERIVED, never stored, built BY the session,
    defined here so this module keeps its one-way dependency.

    NOT an emergency row. For every outcome but a fill, every protection a position
    has — both price triggers, its one exit attempt, the 15:30 flatten — is intact
    here by construction; the row exists so a position still marked at one tick with
    no take going out has a sentence saying why. A `filled` row still held is a
    partial fill's stub, whose close is spent and which the Exit attempts row carries
    in red. The one state here that needs a human is `order_may_be_working`.
    """

    external_identifier: str
    candidate_id: str
    #: `filled` / `unfilled` / `nothing_at_broker` / `not_resolved`.
    outcome: str
    reason: str | None
    #: The take's order was never confirmed terminal: a close may be resting at the
    #: broker, and while it is, the emergency exit's duplicate guard will refuse to
    #: stack a second one on it. The one amber state of this row.
    order_may_be_working: bool
    retired_for_s: float


@dataclass(frozen=True)
class ProfitTargetStatus:
    """The profit target as the RUNNING process has it. (2026-09-23)

    `enabled` is the loaded `watchdog.profit_target_enabled`. Promotion and rollback
    are a config flip and a restart, so — exactly as for the residual recheck's
    `cancel_enabled` — the page is the one place an operator can confirm which mode is
    actually live.
    """

    enabled: bool
    #: Profit takes that closed a position this session.
    taken: int
    retired: tuple[RetiredProfitTargetRow, ...]


@dataclass(frozen=True)
class FlattenDeferralRowStatus:
    """One HELD position the flatten deferral has decided on. (2026-09-25)

    The status-layer twin of the watchdog's `FlattenDeferralRow`, under the same
    construction rule as every row here. NOT an emergency row: a deferred position
    keeps its stop — the underlying trigger, and the credit trigger where it is on —
    live to the close, and the deferral is re-checked every tick. `deferring=False`
    after a deferral means the flatten was released and handed to the exit path —
    `reason` says why.
    """

    external_identifier: str
    candidate_id: str
    deferring: bool
    reason: str
    z: float | None
    p_touch: float | None
    lhs: float | None
    premium: float
    distance_points: float | None
    deferred_for_s: float


@dataclass(frozen=True)
class FlattenDeferralStatus:
    """The flatten deferral as the RUNNING process has it. (2026-09-25)

    `mode`, `k`, `tail_z99` and `tail_alpha` are the loaded values: a change is a
    config-only release and a restart, so — like `ProfitTargetStatus.enabled` — this
    is where an operator confirms which parameters are live.
    """

    mode: str
    k: float
    tail_z99: float
    tail_alpha: float
    rows: tuple[FlattenDeferralRowStatus, ...]


#: At most this many rows per unmanaged category before the rest collapse to
#: "+N more", and at most this many symbols inside one row. Bounds by COUNT, never by
#: characters — the 240-char payload truncation in the audit tail is precisely what
#: these live rows exist to sidestep, so nothing here may reintroduce it. Rendering
#: bounds, so module constants, the category `RATE_WINDOW_S` and
#: `recent_limit` are already in.
MAX_UNMANAGED_ROWS = 8
MAX_UNMANAGED_SYMBOLS = 4


@dataclass(frozen=True)
class UnmanagedPosition:
    """One held position Argus is not managing at all, as of right now.

    The worst state this page can report, and the third instance of one pattern it has
    already been fixed for twice — `daily_loss.breach` rendered TRIPPED off a log row,
    and a spent exit attempt rendering as an ordinary open position. Both times the
    page read calm for a state that needed a human.

    `PositionWatchdog.check_once` iterates `self._scan.spreads`, and an
    `unattributed` / `malformed` entry is by construction not in it. So a position
    here has **no trigger evaluated, no mark, and no `FLATTEN_BEFORE_CLOSE`** — on
    0DTE the last one means it rides to settlement — while still consuming a
    concurrency slot, because `open_spread_equivalent` counts the broker's LEGS. Until
    2026-09-02 the page showed the consumed slot and nothing anywhere said why.

    Strictly worse than both rows above it, which is why this is its own field rather
    than a severity on either. A `blind` impaired position is still scanned, still
    marked, still flattened; a spent exit attempt has been evaluated every tick since
    2026-08-28 and carries its mark. This one has left the loop.

    Same construction rule as every other live row: DERIVED, never stored, built BY the
    session from `PositionWatchdog.unwatchable_positions()`, and defined here rather
    than imported from `execution/` so this module keeps its one-way dependency.
    """

    #: `"unattributed"` or `"malformed"` — two causes, one consequence.
    kind: str
    #: Carried VERBATIM from `store.py`. It is the operator's whole diagnosis, it is
    #: under 70 characters in every shape, and it is never truncated.
    problem: str
    symbols: tuple[str, ...]
    #: `(symbol, one-line explanation)`, pre-flattened by the watchdog because it is
    #: the only place that knows a `malformed` dict's shape. Empty for the shapes that
    #: carry no per-symbol numbers.
    detail: tuple[tuple[str, str], ...]
    candidate_id: str | None
    external_identifier: str | None
    broker_order_id: int | None
    #: The EPISODE's age, shared by every row of one reading.
    unwatchable_for_s: float
    #: The one non-red state: a malformed-only condition inside `watchdog.book_settle_s`
    #: is the ordinary mid-fill window (two legs of one order booking ~0.25s apart), and
    #: `_report_unwatchable` suppresses its own page for exactly it. Mirrors
    #: `PositionEvaluability.blind`'s severity split, for the identical reason —
    #: colouring a routinely-benign condition red is how a red badge stops meaning
    #: anything (2026-08-05). An `unattributed` row is never settling, at any age.
    settling: bool


@dataclass(frozen=True)
class ResidualOrderRow:
    """One order left working at the broker that no walk is driving, right now.

    P5 (2026-08-30) built the registry and its `outstanding()` read and deliberately
    left the rendering out of that pass. This is it — the fourth instance of the
    pattern `UnmanagedPosition` names, one level down: exposure that could still become
    a position, with nothing on the page saying it exists.

    **More useful, not less, while `watchdog.unmanaged_order_cancel = false`.** In that
    mode the recheck records what it WOULD have cancelled and sends nothing, so this row
    and `order.residual_would_cancel` telemetry are the only surfaces showing the
    decision — and the flip to `true` is a config change with no deploy, which makes
    the page the only place an operator can confirm which mode is actually live.

    Same construction rule as the rows above: DERIVED, never stored, built BY the
    session from `ResidualOrderRecheck.residual_rows()`. Calmer than
    `UnmanagedPosition` on purpose — an order is not a position, and P5 re-cancels the
    opening ones once promoted — with one escalation, in `last_sighting`.
    """

    broker_order_id: int
    external_identifier: str
    candidate_id: str | None
    #: MINTED by Argus, never the broker's relabelled leg action, and it is also the
    #: eligibility test: an OPENING order gets the automatic re-cancel and a CLOSING
    #: one never does, because cancelling a close strands a position.
    minted_direction: str
    walk_outcome: str
    cancel_requested: bool
    attempts: int
    working_for_s: float
    last_status: str | None
    #: Three of `ResidualSighting`'s six values are READ GAPS — `absent`, `unreadable`,
    #: `ambiguous` — and P5's rule is that absence is not a verdict in either direction:
    #: the broker will neither confirm the order dead nor license a cancel. That is the
    #: one state this row renders red.
    last_sighting: str | None
    #: `watchdog.unmanaged_order_cancel`, identical on every row of one render. It rides
    #: on the row rather than as a snapshot field so the three-state `None` contract
    #: covers it too: with no session there is no mode, and a top-level boolean would
    #: have to invent `False`.
    cancel_enabled: bool
    #: The order's limit as sent, when known, and whether it is SUPPOSED to be working
    #: (a resting market-crossing close, 2026-09-25). A `working_expected` row is the
    #: cross doing its job, not an abandoned order.
    limit_price: float | None = None
    working_expected: bool = False


@dataclass(frozen=True)
class EntriesEndedToday:
    """Today's entries stood down, while the session kept running (2026-09-01).

    The transient half of a pair whose two members produce the SAME runtime posture
    — entries off, watchdog and exits and the 15:30 flatten on — and are otherwise
    opposites:

      - P3's `HaltGrade.ENTRIES` (the HALTED row, `halt_grade="entries"`) engages
        the kill switch, is process-wide, survives a restart, and clears only when
        a human sends `POST /resume`.
      - THIS engages nothing, is session-scoped, and clears at the next rollover
        with no human step.

    They are two fields and two rows on purpose. A single `monitor_only` flag would
    answer "are entries off?" and lose "what clears it" — and at 15:00 on expiry day
    the operator already knows the first and needs the second. That is the same
    class of error as reading `breaker_tripped` off a `daily_loss.breach` row: a
    true word on the page, a false conclusion off it.

    Same construction rule as `PositionEvaluability` and `UnclosablePosition`:
    DERIVED, never stored, built BY the session from `EntryExecutor.entries_ended`,
    and defined here rather than imported from `execution/` so this module keeps its
    one-way dependency.
    → docs/decisions/2026-09-01-end-day-keeps-the-watchdog.md
    """

    #: `entry_guard_unreachable` (sustained cert unreachability) or
    #: `entry_build_failed` (a code defect) — the same `kind` on `entry.aborted_day`.
    kind: str
    candidate_id: str
    error_type: str
    #: ET wall clock of the stand-down, rendered for a human.
    since: datetime


class SessionStatus:
    """Process-lifetime publisher of the current session's live state.

    The session body writes to this as it moves through its phases and, once the
    loop is up, hands over a `risk_source` — a zero-arg callable returning a fresh
    `RiskSnapshot` (in practice `RiskSnapshotBuilder.build`). Reading it is a pure
    in-memory operation (ages off injected monotonic clocks), safe to call from
    the control handler on the event loop.

    On session end the risk source is CLEARED, so `/status` reports "no active
    session" rather than an ever-growing age from a torn-down feed.

    Everything runs on the single event loop, and the setters/getters do no
    awaiting, so no locking is needed — a reader can never observe a half-applied
    update.
    """

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or et_now
        self._phase = SessionPhase.IDLE
        self._phase_since = self._now()
        self._trading_date: date | None = None
        self._risk_source: Callable[[], RiskSnapshot] | None = None
        self._link_source: Callable[[], LinkHealth] | None = None
        self._evaluability_source: (
            Callable[[], tuple[PositionEvaluability, ...]] | None
        ) = None
        self._spent_exit_source: (
            Callable[[], tuple[UnclosablePosition, ...]] | None
        ) = None
        self._unmanaged_source: (
            Callable[[], tuple[UnmanagedPosition, ...]] | None
        ) = None
        self._residual_source: (
            Callable[[], tuple[ResidualOrderRow, ...]] | None
        ) = None
        self._entries_ended_source: Callable[[], EntriesEndedToday | None] | None = None
        self._watchdog_health_source: Callable[[], WatchdogHealth | None] | None = None
        self._profit_target_source: Callable[[], ProfitTargetStatus | None] | None = None
        self._flatten_deferral_source: (
            Callable[[], FlattenDeferralStatus | None] | None
        ) = None
        self._counts_baseline: dict[str, int] | None = None

    # -- publishing (called by the session body) ---------------------------

    def session_starting(self, trading_date: date) -> None:
        """A trading-day session is beginning: opening broker sessions."""
        self._trading_date = trading_date
        self._risk_source = None
        self._link_source = None
        self._evaluability_source = None
        self._spent_exit_source = None
        self._unmanaged_source = None
        self._residual_source = None
        self._entries_ended_source = None
        self._watchdog_health_source = None
        self._profit_target_source = None
        self._flatten_deferral_source = None
        self._counts_baseline = None
        self._set(SessionPhase.LOGGING_IN)

    def set_reconciling(self) -> None:
        self._set(SessionPhase.RECONCILING)

    def set_idle(self) -> None:
        """No session, and none starting. The PROCESS-start reconcile's bookend.

        Added 2026-08-26 with the reconcile's outer retry. Until then the
        process-start reconcile never published a phase at all, so `/status` read
        `idle` throughout it — harmless while it was one fast round trip, and a
        misreport once it can occupy `[reconcile] retry_budget_s`. This is what
        puts the page back once it is over, on every path including the halting
        ones: `StatusSnapshot.halted` sits above the phase and changes what it
        means, so a latched `reconciling` would have the page claim work is in
        progress on a parked process.
        """
        self._set(SessionPhase.IDLE)

    def session_running(
        self,
        risk_source: Callable[[], RiskSnapshot],
        link_source: Callable[[], LinkHealth] | None = None,
        counts_baseline: dict[str, int] | None = None,
        evaluability_source: Callable[[], tuple[PositionEvaluability, ...]] | None = None,
        spent_exit_source: Callable[[], tuple[UnclosablePosition, ...]] | None = None,
        unmanaged_source: Callable[[], tuple[UnmanagedPosition, ...]] | None = None,
        residual_source: Callable[[], tuple[ResidualOrderRow, ...]] | None = None,
        entries_ended_source: Callable[[], EntriesEndedToday | None] | None = None,
        watchdog_health_source: Callable[[], WatchdogHealth | None] | None = None,
        profit_target_source: Callable[[], ProfitTargetStatus | None] | None = None,
        flatten_deferral_source: Callable[[], FlattenDeferralStatus | None] | None = None,
    ) -> None:
        """The signal loop is live; `risk_source()` yields the current snapshot.

        `link_source` is the same shape and published at the same instant, so the
        two can never disagree about whether a session is up. OPTIONAL, so the
        pre-existing two-line construction in tests and tools keeps working — a
        caller that omits it simply gets `None` link fields, exactly as a caller
        with no session does.

        `evaluability_source` is the same shape again, and optional for a stronger
        reason than convenience: SHADOW HAS NO WATCHDOG. There is no trading side to
        read, so absent here means "not applicable", which `/status` renders
        differently from "every position is fine".

        `spent_exit_source` is the fourth, published at the same instant and off the
        same watchdog, for the same reason: it and the evaluability row are the two
        halves of one position's story — impaired, then out of attempts — and a page
        that could show one without the other would go quiet at the worse of the two.

        `unmanaged_source` and `residual_source` are the fifth and sixth, published at
        the same instant off the same trading side and optional for the same stronger
        reason. They are the two halves of what Argus is HOLDING BUT NOT MANAGING — a
        position the scan could not explain (no trigger, no mark, no flatten) and an
        order a finished walk left working — and both were Tier 1 pages that appeared
        on no surface between pages until 2026-09-02.

        `entries_ended_source` is the seventh, published at the same instant off the
        same trading side, and optional for the same stronger reason: SHADOW HAS NO
        ENTRY EXECUTOR. It returns None while entries are live and an
        `EntriesEndedToday` once the entry path has stood down for the day — the
        state that, before 2026-09-01, could not appear on this page at all because
        it ended the session (see `EntriesEndedToday`).

        `profit_target_source` (2026-09-23) is off the same watchdog, published at the
        same instant and optional for the same stronger reason: shadow has no watchdog,
        so there is no switch to report and no position it could have acted on.

        `flatten_deferral_source` (2026-09-25) likewise: the deferral's loaded mode
        and parameters, and every held position it has decided on.
        """
        self._risk_source = risk_source
        self._link_source = link_source
        self._evaluability_source = evaluability_source
        self._spent_exit_source = spent_exit_source
        self._unmanaged_source = unmanaged_source
        self._residual_source = residual_source
        self._entries_ended_source = entries_ended_source
        self._watchdog_health_source = watchdog_health_source
        self._profit_target_source = profit_target_source
        self._flatten_deferral_source = flatten_deferral_source
        # Snapshot of `AuditLog.counts` taken as the loop came up, so `/status` can
        # report what happened THIS SESSION rather than an all-time ledger. Same
        # baseline, same instant, as the journal's `(+N)` block — see
        # `session._format_counts` for why the distinction is load-bearing.
        # `is not None`, NOT truthiness: an EMPTY baseline is a real baseline (a
        # first-ever session against a fresh audit DB), and treating it as "absent"
        # would silently report no session counts at all for that whole session.
        self._counts_baseline = (
            dict(counts_baseline) if counts_baseline is not None else None
        )
        self._set(SessionPhase.RUNNING)

    def session_finished(self, *, ended: bool) -> None:
        """The session is over. `ended` True = normal close/stop; False = aborted.

        Clears the live risk AND link sources so freshness ages and link counts
        read as "no active session" until the next day's session starts, rather
        than as an ever-growing age off a torn-down feed.

        **The counts baseline deliberately SURVIVES**, and that asymmetry is the
        point. Everything above is a live handle into a feed that no longer exists,
        so reading it after teardown would report an age off a dead stream. The
        baseline is not a handle — it is a static integer snapshot, and the totals
        it subtracts from are read out of the audit DB, which is still there and
        still correct. Clearing it turned a valid record of the day into `{}` at
        exactly the moment it gets read: the EOD review. Observed 2026-08-19 at
        16:41 ET, where `/status` could answer "what happened today?" all afternoon
        and then went blank forty minutes after the close.

        `session_starting` is what replaces it, so between the close and the next
        morning this reports the session that just ended — paired with
        `trading_date`, which is not cleared here either.
        """
        self._risk_source = None
        self._link_source = None
        self._evaluability_source = None
        # Cleared with the rest, and that is the whole of this row's session
        # scoping. A spent attempt is a fact about ONE session's watchdog — the
        # object itself is rebuilt per trading day — so carrying it past the close
        # would make the page a running tally of every position that ever ended
        # badly, which is precisely the process-lifetime-ledger failure the counts
        # block above was fixed for in the opposite direction.
        self._spent_exit_source = None
        # Cleared with it, and for the same reason twice over: the watchdog and the
        # residual registry are BOTH rebuilt per trading day, so a source kept past
        # teardown is a handle into last session's object — and the residual registry
        # is explicitly never rehydrated (P5), so carrying its row past the close
        # would show orders belonging to a process state that no longer exists.
        self._unmanaged_source = None
        self._residual_source = None
        # Cleared with them, and for the same reason, plus one of its own: the latch
        # lives on an `EntryExecutor` that is rebuilt per trading day, so a source
        # kept past teardown would be a handle into last session's object. The day's
        # record survives where it belongs — `entry.aborted_day` and
        # `session.entries_ended` are in the counts block and the audit tail, both of
        # which deliberately outlive the session.
        self._entries_ended_source = None
        # Cleared with the rest: the watchdog whose health this reads is rebuilt per
        # trading day, so a source kept past the close would be a handle into last
        # session's object. The day's episodes survive where they belong — the
        # `watchdog.degraded` / `watchdog.recovered` records in the audit tail.
        self._watchdog_health_source = None
        # Cleared with the rest for the same reason: a handle into a watchdog that is
        # rebuilt per trading day. The day's takes survive in the audit trail
        # (`watchdog.exit_finished` with `trigger_reasons: ["profit_target"]`).
        self._profit_target_source = None
        # Same reason: a handle into a per-day watchdog. The day's deferrals survive
        # in `watchdog.flatten_deferred` / `_undeferred` / `_deferral_outcome`.
        self._flatten_deferral_source = None
        self._set(SessionPhase.ENDED if ended else SessionPhase.ABORTED)

    def _set(self, phase: SessionPhase) -> None:
        if phase is not self._phase:
            self._phase_since = self._now()
        self._phase = phase

    # -- reading (called by StatusService.assemble) ------------------------

    @property
    def phase(self) -> SessionPhase:
        return self._phase

    @property
    def phase_since(self) -> datetime:
        return self._phase_since

    @property
    def trading_date(self) -> date | None:
        return self._trading_date

    def current_risk(self) -> RiskSnapshot | None:
        """The live risk snapshot if a session is running, else None."""
        if self._risk_source is None:
            return None
        return self._risk_source()

    @property
    def counts_baseline(self) -> dict[str, int] | None:
        """`AuditLog.counts` as it stood when the CURRENT OR MOST RECENT session's
        loop came up.

        None before the first session of the process, or when a caller published no
        baseline — `/status` then reports no session counts rather than a delta
        against zero, which would render every all-time total as if it had just
        happened.

        It outlives the session that set it, by design: see `session_finished`.
        """
        return self._counts_baseline

    def current_evaluability(self) -> tuple[PositionEvaluability, ...] | None:
        """Impaired positions right now, or None if no watchdog is publishing.

        None and `()` are DIFFERENT and both are real: None is shadow (or no running
        session) — nothing is being watched, so the question does not apply. `()` is
        a live watchdog reporting every watched position fully evaluable.
        """
        if self._evaluability_source is None:
            return None
        return self._evaluability_source()

    def current_spent_exits(self) -> tuple[UnclosablePosition, ...] | None:
        """Positions with no exit attempt left, right now, or None if no watchdog.

        Same three-state contract as `current_evaluability`, and for the same
        reason: None is "not applicable" (shadow, or no running session), `()` is a
        live watchdog reporting that everything held still has its attempt.
        """
        if self._spent_exit_source is None:
            return None
        return self._spent_exit_source()

    def current_unmanaged(self) -> tuple[UnmanagedPosition, ...] | None:
        """Held positions nothing is managing right now, or None if no watchdog.

        Same three-state contract as `current_evaluability`, and here it carries the
        most weight of the three: None is "not applicable" (shadow, or no running
        session), `()` is a live watchdog reporting that the ledger explains everything
        it holds. On a `HaltGrade.ALL` parked process there is no session, so this
        reads None — correct, and NOT the reassuring reading. The HALTED row above says
        why.
        """
        if self._unmanaged_source is None:
            return None
        return self._unmanaged_source()

    def current_residuals(self) -> tuple[ResidualOrderRow, ...] | None:
        """Orders left working that nobody is driving, or None if no registry.

        Same three-state contract again. `()` means the registry is live and every
        order a walk abandoned has since been seen terminal — which is the common case,
        since a residual that resolves on the very next poll is what P5 expects.
        """
        if self._residual_source is None:
            return None
        return self._residual_source()

    def current_entries_ended(self) -> EntriesEndedToday | None:
        """Why today's entries stood down, or None if they have not.

        Two-state, not three, and the asymmetry with the two above is deliberate:
        there is no distinction worth drawing between "shadow, so the question does
        not apply" and "entries are live". Both mean the row does not render, and
        collapsing them keeps the caller from having to decide what an unrenderable
        third state would say.
        """
        if self._entries_ended_source is None:
            return None
        return self._entries_ended_source()

    def current_watchdog_health(self) -> WatchdogHealth | None:
        """The watchdog's own health right now, or None if no watchdog is publishing.

        Same three-state contract as the rows above, and it is the row that makes a
        DEGRADED-but-running watchdog visible at all: since 2026-09-10 a watchdog
        exception no longer parks the process, so `phase: running` is no longer
        evidence that anything is being watched. `failures > 0` with
        `degraded=False` is the RECOVERED state — watching again, entries still
        halted until `POST /resume`.
        """
        if self._watchdog_health_source is None:
            return None
        return self._watchdog_health_source()

    def current_profit_target(self) -> ProfitTargetStatus | None:
        """The profit target's live state, or None if no watchdog is publishing.

        Same three-state contract as the watchdog rows above: None is "not
        applicable" (shadow, or no running session), and a status with an empty
        `retired` is a live watchdog with nothing to explain.
        """
        if self._profit_target_source is None:
            return None
        return self._profit_target_source()

    def current_flatten_deferral(self) -> FlattenDeferralStatus | None:
        """The flatten deferral if a session published it, else None (shadow, idle)."""
        if self._flatten_deferral_source is None:
            return None
        return self._flatten_deferral_source()

    def current_links(self) -> LinkHealth | None:
        """Live link health if a session published it, else None."""
        if self._link_source is None:
            return None
        return self._link_source()


@dataclass(frozen=True)
class StatusSnapshot:
    """One point-in-time picture of the process. Rendered to JSON and to HTML.

    Freshness/account fields are `None` when no session is active (nothing live to
    measure). `market_data_stale`/`account_data_stale` are the fail-closed
    staleness verdicts (`staleness.is_stale`) against the configured thresholds —
    surfaced alongside the raw age (not instead of it) so a slow drift toward the
    threshold is visible before it trips.
    """

    generated_at: datetime
    environment: str
    #: The running build, `git describe`d at IMAGE BUILD TIME (`argus._version`).
    #: `"unknown"` for an unstamped build — an ad hoc local one, or a `docker compose
    #: build` that omitted the arg — which is a real answer and never an error.
    #: Beside `environment` rather than under `process` on purpose: both are facts
    #: about WHAT is running, and the first question after "is it up?" is "is it the
    #: thing I deployed?".
    version: str
    started_at: datetime
    uptime_s: float

    phase: str
    phase_since: datetime
    phase_age_s: float
    trading_date: date | None
    next_session_open: datetime | None

    market_data_age_s: float | None
    market_data_max_age_s: float
    market_data_stale: bool | None
    account_data_age_s: float | None
    account_data_max_age_s: float
    account_data_stale: bool | None
    # Link health (None when no session is active). SESSION-scoped counts — the
    # adapters are rebuilt daily — deliberately unlike the journal's all-time
    # block. `*_reconnects` is a recovered-from count, not a fault: a link that
    # dropped and came back is the reconnect logic working. What is actionable is
    # `*_connected` being False right now, or `account_refresh_consecutive_failures`
    # climbing, which leads `account_data_age_s` toward blocking entries.
    market_data_connected: bool | None
    market_data_reconnects: int | None
    account_connected: bool | None
    account_reconnects: int | None
    account_refresh_consecutive_failures: int | None

    account_nlv: float | None
    daily_pnl: float | None
    #: BROKER LEGS. A vertical is two, so this reads 2 for one open spread — which
    #: is exactly the confusion that produced the 2026-08-13 gate bug, hence the
    #: spread-equivalent beside it and both units on the phone view.
    open_position_count: int | None
    open_spread_equivalent: int | None
    #: OPENING orders still working at the broker, per trade idea — counted toward
    #: `max_concurrent_positions` on top of the filled positions (Finding 6,
    #: 2026-08-28). Folded into `open_spread_equivalent` above, so this is here to
    #: keep that number readable when it exceeds the position count.
    working_open_spread_count: int | None
    #: Positions with an unevaluable trigger right now. None = no watchdog publishing
    #: (shadow, or no running session); () = a live watchdog with nothing impaired.
    impaired_positions: tuple[PositionEvaluability, ...] | None
    #: Positions still held whose ONE exit attempt is spent — no trigger evaluated,
    #: nothing automated left to try. Same None/`()` contract as the row above, and
    #: deliberately a SEPARATE field rather than a severity on it: a spent position
    #: has already left `impaired_positions`, which is exactly why this exists.
    spent_exit_attempts: tuple[UnclosablePosition, ...] | None
    #: Held positions the scan could not explain — no trigger evaluated, no mark, no
    #: 15:30 flatten. Same None/`()` contract as the two rows above, and the most
    #: serious of the three: those describe a position the watchdog is struggling with,
    #: this one describes a position the watchdog does not have.
    unmanaged_positions: tuple[UnmanagedPosition, ...] | None
    #: Orders a finished walk left working at the broker. Same contract; the only row
    #: in this group about an ORDER rather than a position, which is also why it is the
    #: calmest.
    residual_orders: tuple[ResidualOrderRow, ...] | None
    #: The watchdog's OWN health (2026-09-10) — distinct from every row above, which
    #: describes a position or an order this watchdog holds. Since 2026-09-10 a
    #: watchdog exception no longer parks the process: it halts ENTRIES and keeps
    #: watching, so `phase: running` is no longer evidence that anything is watched.
    #: None = no watchdog publishing (shadow, or no running session).
    watchdog_health: WatchdogHealth | None
    #: The profit target (2026-09-23): whether the running process may send one, how
    #: many closed a position this session, and which held positions it has left.
    #: None = no watchdog publishing (shadow, or no running session).
    profit_target: ProfitTargetStatus | None

    heartbeat_last_lag_s: float
    heartbeat_max_lag_s: float
    heartbeat_breach_count: int
    heartbeat_sustained_events: int

    # Memory + GC, added 2026-08-05 to diagnose the intra-session lag ramp: loop
    # lag climbs ~+0.21s/hour from ~0.07s at the open to ~1.3s mean (2.2s peak) by
    # 15:00, identically on every session measured (Jul 28 - Aug 4), and RESETS on
    # process restart. That reset is what points at accumulation rather than at
    # load or market conditions. `rss_at_start_bytes` is captured when this service
    # is built during `startup()`, so the growth figure is readable from ONE poll
    # instead of requiring the operator to difference two. None where /proc is
    # unavailable (non-Linux dev machines) — never an error.
    rss_bytes: int | None
    rss_at_start_bytes: int | None
    gc_counts: tuple[int, int, int]
    gc_collections: tuple[int, int, int]

    audit_last_seq: int
    audit_queue_depth: int
    audit_queue_alert_depth: int
    audit_queue_maxsize: int
    audit_dropped: int
    audit_failed: str | None

    killswitch_engaged: bool
    killswitch_reason: str | None
    killswitch_source: str | None
    killswitch_since: str | None

    #: Whether the halt in force was INHERITED from a previous process, read live
    #: off the switch (2026-09-01). The single most decision-relevant fact after a
    #: restart, and one the other four fields cannot express: `since` is the
    #: original press either way, so without this the page cannot distinguish "this
    #: process was killed at 10:00" from "this process came up on Friday's halt".
    #: False whenever nothing is engaged.
    killswitch_restored: bool

    #: Whether the daily-loss breaker is tripped RIGHT NOW — read straight off the
    #: live `DailyLossBreaker` that `PreTradeGate` consults, never inferred from a
    #: `daily_loss.breach` row.
    #:
    #: The row and the rail are different facts and they CAN disagree, in the worst
    #: possible direction. Until 2026-08-20 this was derived from "was a breach
    #: logged today?", so a same-day restart — where the row survives and the
    #: in-memory latch did not — rendered TRIPPED in red on the page while the gate
    #: was still authorizing entries. An operator glancing at `/status` under
    #: pressure read a halt that was not happening. Rehydration (`risk.rehydrate`)
    #: closed the underlying gap, but deriving live state from an indirect source is
    #: the fragility itself: the same defect had already been fixed one field over,
    #: for `killswitch_source`/`killswitch_since`.
    #:
    #: None means NO BREAKER IS ARMED FOR TODAY, which is neither True nor False:
    #: no session has built one yet (before the process's first session, or a halted
    #: process that never got that far), or the one still held belongs to a previous
    #: trading day. Trading-date scoping is what keeps a Tuesday trip off Wednesday
    #: morning's page — the latch is per trading date, so the report of it is too.
    breaker_tripped: bool | None

    # HALTED is strictly stronger than an engaged kill switch, and the difference
    # is what an operator actually needs from this page. Engaged means "no new
    # entries, until a human clears it with POST /resume" — a `/kill` at 10:00
    # leaves a process that is otherwise entirely healthy, watching and flattening.
    # Halted means additionally "no further sessions, and a restart will not help"
    # — a rail refused, and a human has to resolve the cause. A halted process is
    # deliberately still alive and still answering here (2026-08-17); before that
    # it exited, and this page went with it.
    #
    # **The engaged half used to read "a `/kill` at 10:00 leaves a process that
    # resumes tomorrow", and that stopped being true on 2026-09-01**: the halt is
    # now durable, so it survives the rollover AND the restart, and only `/resume`
    # clears it.
    halted: bool
    halt_reason: str | None
    halt_source: str | None
    halted_since: str | None
    #: How WIDE the halt reaches (`HaltGrade`), or None when nothing is halted.
    #: `"all"` is the parked process this page has always described. `"entries"`
    #: (2026-08-29) is equally permanent and equally un-enterable, and differs in
    #: one way that matters to whoever is reading this at 15:00 on expiry day: the
    #: session is still running, so held positions still have a watchdog, still get
    #: marked, and still get flattened before the close. Rendered as part of the
    #: HALTED row rather than beside it — a second row would invite reading them as
    #: two conditions, and softening the word "HALTED" for the narrow grade would
    #: misreport a process that will not trade again without a human.
    halt_grade: str | None

    #: Today's entries stood down while the session kept running (2026-09-01), or
    #: None while they are live. THE OTHER monitor-only state, and deliberately its
    #: own field rather than a share of `halt_grade`: see `EntriesEndedToday` for
    #: why "what clears this" is the question the page owes and a merged
    #: `monitor_only` boolean cannot answer. Both may be set at once — a transient
    #: stand-down followed by a halt — and both render, HALTED first because it is
    #: the stronger. The reverse order cannot arise: a halted process comes up with
    #: no submit hook at all, so the entry path is never called.
    entries_ended: EntriesEndedToday | None

    #: How much life the macro-event blackout list has left, or None when the caller
    #: published nothing (every construction site predating the field). Rendered
    #: SILENT while the runway is healthy — a permanent banner for a condition that is
    #: fine 11 months of the year trains the eye to skip the row.
    blackout: BlackoutRunway | None

    # RAW count of shadow.would_submit records for the current ET date. NOT the
    # deduped distinct-trade-idea count tools.mark computes — a 5s cadence
    # re-proposes the same spread all day, so this number is inflated by design
    # and is only a "is the loop finding candidates" pulse, not a trade count.
    would_submit_today_raw: int

    # Most-recent-first audit tail: each dict has seq, ts, tier, category, payload
    # (payload is the raw JSON string as stored).
    recent: tuple[dict[str, Any], ...]

    #: Audit categories that MOVED this session, mapped to how much they moved.
    #: Empty only before the process's FIRST session (or when none published a
    #: baseline) — it survives session end, so the EOD read still answers "what
    #: happened today?". See `SessionStatus.session_finished`.
    #:
    #: DELTA, not all-time, and that is the whole point. An all-time ledger is what
    #: `session._format_counts` had to be fixed for: `account.refresh_failed=285`
    #: frozen for an hour and `285` climbing every tick render identically, and the
    #: ambiguity produced three false "the system is failing" diagnoses in one day.
    #: Categories at zero delta are OMITTED, so this is a short list of what has
    #: actually happened today.
    #:
    #: It also fills a real gap: `would_submit_today_raw` counts one SHADOW-only
    #: category, so it reads 0 forever in sandbox and live — the only environments
    #: where orders exist. Before this, the phone-reachable surface could not answer
    #: "has anything executed today?" at all, while the journal could.
    session_counts: dict[str, int] = field(default_factory=dict)

    #: Unconsumed events sitting in each SDK streamer's UNBOUNDED buffer, per type.
    #: Defaulted (and therefore last) so every existing construction site keeps
    #: working. A climbing depth is the lag ramp's accumulator; near-zero
    #: exonerates the streaming path.
    market_data_queue_depths: dict[str, int] = field(default_factory=dict)
    account_queue_depths: dict[str, int] = field(default_factory=dict)

    #: WHICH CONFIG IS LOADED — the version stamp's missing half.
    #:
    #: `version` answers "which code?", and the image is what a rollback moves.
    #: Config is bind-mounted and DELIBERATELY does not roll back with the image
    #: tag (docker-compose.yml says so), so after a config-only release nothing on
    #: this page could distinguish "the new config was loaded" from "the restart
    #: happened before the checkout" from "somebody hand-edited the droplet and it
    #: has not restarted since". These three fields do.
    #:
    #: Three scalars rather than a `ConfigSource` object, deliberately: this module
    #: imports no config loader (see `StatusService`), and `version` /
    #: `environment` / the staleness bounds are already scalars lifted off the
    #: config by `Application.startup` for exactly that reason.
    #:
    #: `None` is a real value — a `Config.from_dict` construction (tests, tools)
    #: has no file behind it — and renders as such rather than as an invented
    #: default. `config_profile` is separately `None` for a path that is not named
    #: `argus.<profile>.toml`.
    config_profile: str | None = None
    config_filename: str | None = None
    config_sha: str | None = None

    #: WHICH CREDENTIAL SLOT each session authenticated with — the same question
    #: one layer further out (2026-09-09). `version` answers "which code?",
    #: `config_*` "which bytes?", and until the (role, world) rename nothing could
    #: answer "which tastytrade REGISTRATION?" at all: the trading session read one
    #: unprefixed pair in every environment, so going live sent cert values to
    #: production and the only symptom was a login failure ten minutes later.
    #:
    #: Variable-name PREFIXES (`TASTYTRADE_TRADING_CERT_*`), never values, and
    #: scalars for the same reason the three above are: this module imports no
    #: config loader and no broker layer — `Application.startup` lifts them off the
    #: config, which is where every other scalar here comes from.
    credential_slot_trading: str | None = None
    credential_slot_marketdata: str | None = None

    #: WHICH ACCOUNT, one layer further out again. `version` answers "which code?",
    #: `config_*` "which bytes?", `credential_slot_*` "which tastytrade
    #: REGISTRATION?" — and a registration can hold several accounts, so none of
    #: them answers "which of them is this process trading?".
    #:
    #: `None` is a real reading and the NORMAL one in cert: unpinned means
    #: accounts[0]. It is UNREACHABLE under live, where the loader refuses. A
    #: scalar for the same reason every other field here is one — this module
    #: imports no config loader.
    broker_account_number: str | None = None

    #: `RiskSnapshot.daily_pnl_anchored`. When False, `daily_pnl` is rendered as
    #: null / UNANCHORED rather than as the placeholder 0.0 it holds (2026-09-18).
    daily_pnl_anchored: bool | None = None
    #: `RiskSnapshot.loss_budget` (2026-09-21): what `daily_max_loss` still allows a
    #: new entry to put at risk, and which estimate says so. None when no session is
    #: publishing a risk snapshot. Read off the SAME snapshot the gate is handed, so
    #: the page cannot disagree with the rail about why entries stopped — otherwise
    #: this rule's only observable is entries not happening.
    loss_budget: LossBudget | None = None
    #: The flatten deferral (2026-09-25): the running process's mode and parameters,
    #: and every held position it has decided on. None = no watchdog publishing.
    #: Defaulted so every existing construction site keeps working.
    flatten_deferral: FlattenDeferralStatus | None = None
    #: RSS growth and gen-2 collections per hour over the TRAILING window
    #: (`TrailingRates`), and the span actually measured. None until one full window
    #: has been sampled — never `cumulative / uptime`, which carries the startup cost
    #: as a bias that decays and is never removed (2026-09-01 record). Captured by the
    #: service rather than derived here because the history lives in the service.
    rss_growth_per_hour: float | None = None
    gc_gen2_per_hour: float | None = None
    rate_window_s: float | None = None

    # -- derived (pure, shared by both renderings) --------------------------

    @property
    def residual_cancel_mode(self) -> str | None:
        """`"cancel"` / `"would_cancel"`, or None when no registry is publishing.

        Read off the ROWS, which carry it, rather than stored as a snapshot field —
        so it cannot be reported for a session that has no recheck, and it cannot
        disagree with the rows it describes. `()` with a live registry is `None` too:
        the mode is real then, but with nothing to apply it to there is nothing to say,
        and the row already renders "none working".
        """
        if not self.residual_orders:
            return None
        return "cancel" if self.residual_orders[0].cancel_enabled else "would_cancel"

    @property
    def blackout_state(self) -> str | None:
        """`ok` / `expiring` / `exhausted` / `unreadable` / `disabled`, or None.

        Derived against `generated_at` rather than captured, so a process that has
        been up for six weeks reports the runway as it is NOW.
        """
        if self.blackout is None:
            return None
        return self.blackout.state(self.generated_at.date())

    @property
    def blackout_days_remaining(self) -> int | None:
        if self.blackout is None:
            return None
        return self.blackout.days_remaining(self.generated_at.date())

    @property
    def blackout_today(self) -> bool | None:
        """Is the ET date of this snapshot a blackout day? None = cannot say.

        Derived against `generated_at`, never captured, for `blackout_state`'s reason.
        """
        if self.blackout is None:
            return None
        return self.blackout.covers(self.generated_at.date())

    @property
    def blackout_next_session(self) -> bool | None:
        """Is the NEXT session's date a blackout day? None = cannot say.

        A separate question from `blackout_today` because `next_session_open` is
        tomorrow's open while a session is running. **`next_session_open` itself is
        deliberately left blackout-blind**: a blackout day still opens a session —
        login, reconcile, the watchdog, exits and the 15:30 flatten all run — so a
        field that skipped it would misreport when the process next wakes. This says
        what that session will not do, beside the field that says when it starts.
        """
        if self.blackout is None or self.next_session_open is None:
            return None
        return self.blackout.covers(self.next_session_open.date())

    @property
    def rss_growth_bytes(self) -> int | None:
        if self.rss_bytes is None or self.rss_at_start_bytes is None:
            return None
        return self.rss_bytes - self.rss_at_start_bytes

    # -- rendering: JSON ---------------------------------------------------

    def to_json_dict(self) -> dict[str, Any]:
        """Machine-readable form. Non-finite ages collapse to null (a
        never-connected stream reports +inf); `phase` and the `*_stale` flags
        disambiguate "no session" from "session up but data missing"."""
        return {
            "generated_at": self.generated_at.isoformat(),
            "environment": self.environment,
            # Top level, beside `environment`: a script asking "what is deployed?"
            # should not have to know it lives under `process`.
            "version": self.version,
            # Beside `version` for the same reason, and because the pair is the
            # whole answer: `version` is the image, `config` is the half that does
            # not roll back with it. A deploy asserting "did my config load?" reads
            # `config.sha` from here.
            "config": {
                "profile": self.config_profile,
                "file": self.config_filename,
                "sha": self.config_sha,
            },
            # Beside `config` because it completes the same question — which code,
            # which bytes, which registration. Names only; a value never reaches
            # this payload.
            "credentials": {
                "trading": self.credential_slot_trading,
                "market_data": self.credential_slot_marketdata,
            },
            # The first two members of the `account` block, deliberately ahead of
            # its measurements: they close the same sequence `credentials` starts
            # — which code, which bytes, which registration, which ACCOUNT —
            # before the block answers what that account holds. `pinned` is
            # separate from `number` so a script can assert the RAIL without
            # knowing the value: "is this process pinned?" is the question a live
            # deploy asks (design §3.5, 2026-09-10).
            "process": {
                "started_at": self.started_at.isoformat(),
                "uptime_s": round(self.uptime_s, 1),
                "rss_bytes": self.rss_bytes,
                "rss_at_start_bytes": self.rss_at_start_bytes,
                "rss_growth_bytes": self.rss_growth_bytes,
                # Growth per hour over the TRAILING `rate_window_s` — the number to
                # compare against the lag ramp. Null for the process's first hour:
                # a slope needs a full window, and dividing the since-start delta by
                # uptime reported the startup cost as a leak (2026-09-01).
                "rss_growth_bytes_per_hour": _num_or_none(self.rss_growth_per_hour),
                "gc_counts": list(self.gc_counts),
                "gc_collections": list(self.gc_collections),
                # Gen-2 collections per hour, same window. If this tracks the lag
                # ramp, the stall is GC and the next question is what is retained.
                "gc_gen2_per_hour": _num_or_none(self.gc_gen2_per_hour),
                "rate_window_s": _num_or_none(self.rate_window_s),
            },
            "session": {
                "phase": self.phase,
                "phase_since": self.phase_since.isoformat(),
                "phase_age_s": round(self.phase_age_s, 1),
                "trading_date": _date_iso(self.trading_date),
                "next_session_open": _dt_iso(self.next_session_open),
            },
            "market_data": {
                "age_s": _num_or_none(self.market_data_age_s),
                "max_age_s": self.market_data_max_age_s,
                "stale": self.market_data_stale,
                "connected": self.market_data_connected,
                "reconnects": self.market_data_reconnects,
                "queue_depths": self.market_data_queue_depths,
            },
            "account_data": {
                "age_s": _num_or_none(self.account_data_age_s),
                "max_age_s": self.account_data_max_age_s,
                "stale": self.account_data_stale,
                "connected": self.account_connected,
                "reconnects": self.account_reconnects,
                "refresh_consecutive_failures": self.account_refresh_consecutive_failures,
                "queue_depths": self.account_queue_depths,
            },
            "account": {
                "pinned": self.broker_account_number is not None,
                "number": self.broker_account_number,
                "nlv": _num_or_none(self.account_nlv),
                # null, never 0.0, when the day has no P&L anchor: that zero is a
                # placeholder, and the runbook's day-1 cross-check reads this field.
                "daily_pnl": (
                    None if self.daily_pnl_anchored is False
                    else _num_or_none(self.daily_pnl)
                ),
                "daily_pnl_anchored": self.daily_pnl_anchored,
                "open_position_count": self.open_position_count,   # broker LEGS
                "open_spread_equivalent": self.open_spread_equivalent,
                "working_open_spread_count": self.working_open_spread_count,
            },
            "watchdog": {
                # The watchdog's own health, in the same three states the HTML row
                # renders (2026-09-10). null = no watchdog publishing (shadow / no
                # session); a `degraded: true` object is the one state that says the
                # rows below it are as of the last clean pass.
                "health": None if self.watchdog_health is None else {
                    "degraded": self.watchdog_health.degraded,
                    "watching": self.watchdog_health.watching,
                    "failures": self.watchdog_health.failures,
                    "where": self.watchdog_health.where,
                    "error_type": self.watchdog_health.error_type,
                    "error": self.watchdog_health.error,
                    "degraded_for_s": _num_or_none(
                        self.watchdog_health.degraded_for_s
                    ),
                    "last_failure_for_s": _num_or_none(
                        self.watchdog_health.last_failure_for_s
                    ),
                    "last_clean_pass_s": _num_or_none(
                        self.watchdog_health.last_clean_pass_s
                    ),
                    "affected": list(self.watchdog_health.affected),
                },
                # null = no watchdog publishing (shadow / no session); [] = live and
                # everything evaluable. The distinction is load-bearing: "nothing is
                # wrong" and "nothing is watching" must not render the same.
                "impaired_positions": None if self.impaired_positions is None else [
                    {
                        "external_identifier": p.external_identifier,
                        "candidate_id": p.candidate_id,
                        "unknown_reasons": list(p.unknown_reasons),
                        "blind": p.blind,
                        "impaired_for_s": round(p.impaired_for_s, 1),
                    }
                    for p in self.impaired_positions
                ],
                # Same null-vs-[] contract, same reason. A non-empty list here is
                # the most serious thing this document can report about a position:
                # the stop already fired, the one exit attempt is gone, and nothing
                # automated will try again.
                "spent_exit_attempts": None if self.spent_exit_attempts is None else [
                    {
                        "external_identifier": p.external_identifier,
                        "candidate_id": p.candidate_id,
                        "contracts": p.contracts,
                        "outcome": p.outcome,
                        "spent_for_s": round(p.spent_for_s, 1),
                        # The live mark. Null-preserving rather than defaulted: a
                        # 0.0 cost-to-close on a position someone is deciding whether
                        # to close by hand is not a missing value, it is a wrong one.
                        "cost_to_close": p.cost_to_close,
                        # Null while the credit trigger is off, on purpose — the flag
                        # beside it says which null this is (2026-09-27).
                        "credit_stop": p.credit_stop,
                        "credit_trigger_enabled": p.credit_trigger_enabled,
                        "still_triggered": p.still_triggered,
                        # The crash sentence, when a crash is what spent the attempt
                        # (2026-09-10). Null-preserving like the three above.
                        "crash_error": p.crash_error,
                        # The stub-close allowance (2026-09-24). Null-preserving
                        # like the mark: null is "not a stub", a real state, not
                        # a missing value.
                        "stub_close": p.stub_close,
                        # A close still working at the broker (2026-09-25).
                        "working_close_order_id": p.working_close_order_id,
                        "working_close_limit": p.working_close_limit,
                        "working_close_for_s": (
                            None if p.working_close_for_s is None
                            else round(p.working_close_for_s, 1)
                        ),
                    }
                    for p in self.spent_exit_attempts
                ],
                # The profit target (2026-09-23). null = no watchdog publishing; an
                # object's `enabled` is the RUNNING process's switch, which is the one
                # place a config-flip promotion can be confirmed live.
                "profit_target": None if self.profit_target is None else {
                    "enabled": self.profit_target.enabled,
                    "taken": self.profit_target.taken,
                    "retired": [
                        {
                            "external_identifier": r.external_identifier,
                            "candidate_id": r.candidate_id,
                            "outcome": r.outcome,
                            "reason": r.reason,
                            "order_may_be_working": r.order_may_be_working,
                            "retired_for_s": round(r.retired_for_s, 1),
                        }
                        for r in self.profit_target.retired
                    ],
                },
                # The flatten deferral (2026-09-25). null = no watchdog publishing; the
                # object's mode and parameters are what the RUNNING process loaded.
                "flatten_deferral": None if self.flatten_deferral is None else {
                    "mode": self.flatten_deferral.mode,
                    "k": self.flatten_deferral.k,
                    "tail_z99": self.flatten_deferral.tail_z99,
                    "tail_alpha": self.flatten_deferral.tail_alpha,
                    "positions": [
                        {
                            "external_identifier": r.external_identifier,
                            "candidate_id": r.candidate_id,
                            "deferring": r.deferring,
                            "reason": r.reason,
                            "z": r.z,
                            "p_touch": r.p_touch,
                            "lhs": r.lhs,
                            "premium": r.premium,
                            "distance_points": r.distance_points,
                            "deferred_for_s": round(r.deferred_for_s, 1),
                        }
                        for r in self.flatten_deferral.rows
                    ],
                },
            },
            # ONE section for "held but not managed", two lists inside it — because the
            # DECISION is one and the two ACTIONS are not. A position here needs closing
            # by hand; an order here needs a cancel decision, and P5 may already be
            # making it. Merging them would make the reader re-derive that split from
            # each entry's shape at the moment they have least attention.
            "unmanaged": {
                # null = nothing publishing (shadow / no session / parked process);
                # [] = live and the ledger explains everything held. Same load-bearing
                # distinction as `watchdog.impaired_positions`.
                "positions": None if self.unmanaged_positions is None else [
                    {
                        "kind": p.kind,
                        "problem": p.problem,
                        "symbols": list(p.symbols),
                        "detail": [list(d) for d in p.detail],
                        "candidate_id": p.candidate_id,
                        "external_identifier": p.external_identifier,
                        "broker_order_id": p.broker_order_id,
                        "unwatchable_for_s": round(p.unwatchable_for_s, 1),
                        "settling": p.settling,
                    }
                    for p in self.unmanaged_positions
                ],
                "orders": None if self.residual_orders is None else [
                    {
                        "broker_order_id": r.broker_order_id,
                        "external_identifier": r.external_identifier,
                        "candidate_id": r.candidate_id,
                        "minted_direction": r.minted_direction,
                        "walk_outcome": r.walk_outcome,
                        "cancel_requested": r.cancel_requested,
                        "attempts": r.attempts,
                        "working_for_s": round(r.working_for_s, 1),
                        "last_status": r.last_status,
                        "last_sighting": r.last_sighting,
                    }
                    for r in self.residual_orders
                ],
                # Echoed at section level for a script that only wants the mode, and
                # DERIVED from the rows rather than stored twice. null when there is no
                # session, because then there is no recheck to have a mode.
                "cancel_mode": self.residual_cancel_mode,
            },
            "heartbeat": {
                "last_lag_s": round(self.heartbeat_last_lag_s, 4),
                "max_lag_s": round(self.heartbeat_max_lag_s, 4),
                "breach_count": self.heartbeat_breach_count,
                "sustained_events": self.heartbeat_sustained_events,
            },
            "audit": {
                "last_seq": self.audit_last_seq,
                "tier2_queue_depth": self.audit_queue_depth,
                "tier2_queue_alert_depth": self.audit_queue_alert_depth,
                "tier2_queue_maxsize": self.audit_queue_maxsize,
                "tier2_dropped": self.audit_dropped,
                "failed": self.audit_failed,
                # Per-category movement THIS SESSION. See `session_counts`.
                "session_counts": dict(sorted(self.session_counts.items())),
            },
            "kill_switch": {
                "engaged": self.killswitch_engaged,
                "reason": self.killswitch_reason,
                "source": self.killswitch_source,
                "since": self.killswitch_since,
                # "this halt predates the process you are looking at" — see
                # `killswitch_restored`. Always present so a script can rely on it.
                "restored": self.killswitch_restored,
            },
            "halted": {
                "halted": self.halted,
                "reason": self.halt_reason,
                "source": self.halt_source,
                "since": self.halted_since,
                "grade": self.halt_grade,
            },
            # The OTHER monitor-only state, and never merged with the one above.
            # `clears` is spelled out rather than left to be inferred from the
            # absence of `halted`, because that is the whole difference between the
            # two and a script should not have to reconstruct it.
            "entries_ended": (
                None
                if self.entries_ended is None
                else {
                    "kind": self.entries_ended.kind,
                    "candidate_id": self.entries_ended.candidate_id,
                    "error_type": self.entries_ended.error_type,
                    "since": self.entries_ended.since.isoformat(),
                    "watchdog": "running",
                    "clears": "next_trading_day",
                }
            ),
            # `tripped: null` is a real answer, not a missing one: no breaker is
            # armed for today. See `breaker_tripped`.
            "daily_loss_breaker": {"tripped": self.breaker_tripped},
            # null = no session publishing. `remaining` is the number the gate
            # refuses past; `binding` says which estimate set it ("unknown" = zero,
            # for `unknown_reason`).
            "loss_budget": _loss_budget_json(self.loss_budget),
            # null when the caller published nothing. `state` is the one word a
            # script should branch on; the rest is what a human needs to act.
            "event_blackout": (
                None
                if self.blackout is None
                else {
                    "state": self.blackout_state,
                    "enabled": self.blackout.enabled,
                    "last_date": _date_iso(self.blackout.last_date),
                    "days_remaining": self.blackout_days_remaining,
                    "date_count": self.blackout.date_count,
                    "warn_within_days": self.blackout.warn_days,
                    "error": self.blackout.error,
                    # `state` is about the FILE; these two are about the DAYS. On a
                    # blackout day `state` still reads `ok`, which is correct and was,
                    # on its own, the whole problem.
                    "today_blacked_out": self.blackout_today,
                    "next_session_blacked_out": self.blackout_next_session,
                }
            ),
            "shadow": {"would_submit_today_raw": self.would_submit_today_raw},
            "recent": [
                {
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "tier": r["tier"],
                    "category": r["category"],
                    "payload": _safe_json(r["payload"]),
                }
                for r in self.recent
            ],
        }

    # -- rendering: HTML ---------------------------------------------------

    def to_html(self) -> str:
        """Plain, dependency-free HTML: no external JS/CSS/fonts, phone-legible.

        Every dynamic value is `html.escape`d — audit payloads carry arbitrary
        strings (symbols, error text), so escaping is correctness, not just
        hygiene. A manual reload is the refresh model; at single-operator scale a
        polling loop would be complexity solving a problem that doesn't exist.
        """
        # RESTORED rides in the badge, not only in the detail: at a glance it is
        # the difference between a halt this process took and one it inherited,
        # and the second one usually means nobody has looked yet.
        if not self.killswitch_engaged:
            ks_cls, ks_txt = ("ok", "armed (not engaged)")
        elif self.killswitch_restored:
            ks_cls, ks_txt = ("bad", "ENGAGED (RESTORED)")
        else:
            ks_cls, ks_txt = ("bad", "ENGAGED")
        # Three states, not two. "not armed" is NOT "not tripped": it says no
        # session has built today's breaker, so there is no rail to report on (and
        # nothing entering either). Collapsing it into the green reading would be
        # the same over-claim this field was fixed to stop making.
        if self.breaker_tripped is None:
            brk_cls, brk_txt = ("muted", "not armed (no session)")
        else:
            brk_cls, brk_txt = (
                ("bad", "TRIPPED") if self.breaker_tripped else ("ok", "armed (not tripped)")
            )
        rows = [
            _row("Environment", _esc(self.environment)),
            # Directly under it, and before the phase: "which build is this?" is a
            # question about the same thing `environment` answers, and an operator
            # reading this page after a deploy asks both in one glance.
            _row("Build", _version_cell(self.version)),
            # Directly under Build, because the two are one answer to "what is
            # deployed?" and are deliberately allowed to disagree: on a config-only
            # release the image is untouched, so Build legitimately names the
            # PREVIOUS tag while Config names this one's bytes.
            _row(
                "Config",
                _config_cell(self.config_profile, self.config_filename, self.config_sha),
            ),
            # Under Config, closing the same sequence: which code, which bytes,
            # which tastytrade registration. Plain text — there is no bad state to
            # colour, because a wrong slot cannot start (`trading_credentials`
            # refuses a live profile with no live slot, and refuses one carrying
            # cert values); this row exists so the right state is CONFIRMABLE.
            _row(
                "Credentials",
                _credentials_cell(
                    self.credential_slot_trading, self.credential_slot_marketdata
                ),
            ),
            # Immediately after Credentials, closing the same sequence: which
            # code, which bytes, which registration, WHICH ACCOUNT. This is the
            # row an operator reads back at the cutover to confirm the pin took
            # (design §3.5, 2026-09-10).
            _row("Account", _account_cell(self.broker_account_number)),
            _row("Phase", _badge(_PHASE_CLASS.get(self.phase, "muted"), self.phase)),
            # FIRST after the phase when it applies, because it changes the meaning
            # of everything below it: a halted process's freshness and account
            # fields are frozen at the moment it stopped, not live.
            *(
                [
                    _row(
                        "HALTED",
                        # The badge changes wording, never severity. Both grades mean
                        # "will not enter again without a human"; the narrow one adds
                        # what is STILL RUNNING, which is the fact an operator needs
                        # at 15:00 on expiry day and cannot infer from "HALTED".
                        _badge(
                            "bad",
                            "WILL NOT TRADE"
                            if self.halt_grade != "entries"
                            else "NO ENTRIES — watchdog still running",
                        )
                        + f' <span class="muted">{_esc(self.halt_reason or "")}'
                        f' (source={_esc(self.halt_source or "?")},'
                        f' since {_esc(self.halted_since or "?")})</span>'
                        + (
                            ' <span class="muted">— restart required once resolved</span>'
                            if self.halt_grade != "entries"
                            else ' <span class="muted">— MONITOR-ONLY: held positions'
                            " are still watched, closed and flattened. Restart"
                            " required once resolved</span>"
                        ),
                    )
                ]
                if self.halted
                else []
            ),
            # AFTER the HALTED row when both apply, for the reason that one is
            # placed first: HALTED is the stronger claim and it changes the meaning
            # of everything below it. Before "Phase since" either way, because like
            # HALTED this qualifies the phase directly above it — `running` is true
            # and, on its own, misleading.
            #
            # `warn`, not `bad`. The two words that matter are already in the badge,
            # and spending HALTED's red on a condition that resolves itself
            # overnight is how a red badge stops meaning anything.
            *(
                [
                    _row(
                        "Entries",
                        _badge("warn", "ENDED FOR TODAY — watchdog still running")
                        + f' <span class="muted">{_esc(self.entries_ended.kind)}'
                        f' ({_esc(self.entries_ended.error_type)},'
                        f' candidate={_esc(self.entries_ended.candidate_id)},'
                        f' since {_esc(_fmt_dt(self.entries_ended.since))})</span>'
                        # THE sentence. HALTED's equivalent clause reads "restart
                        # required once resolved"; the difference between the two
                        # states is the only thing an operator has to act on.
                        + ' <span class="muted">— MONITOR-ONLY for the rest of'
                        " today: held positions are still watched, closed and"
                        " flattened. Clears automatically at the next trading day;"
                        " no action required.</span>",
                    )
                ]
                if self.entries_ended is not None
                else []
            ),
            # A blackout day, in the same place and under the same label as the
            # stand-down above, because it qualifies the phase the same way: `idle`
            # or `running` is true and, on a blackout day, misleading on its own.
            # Only when the stand-down row is absent — that row already says "no
            # entries" and is the stronger claim.
            #
            # `muted`, not `warn`: planned, expected, nothing to do. It is a BADGE so
            # it cannot be missed, and grey so it does not spend amber on a day that
            # is going exactly as designed.
            *(
                [
                    _row(
                        "Entries",
                        _badge("muted", "NONE TODAY — event blackout")
                        + ' <span class="muted">— the session still runs: reconcile,'
                        " the watchdog, exits and the pre-close flatten. No action"
                        " required.</span>",
                    )
                ]
                if self.blackout_today and self.entries_ended is None
                else []
            ),
            _row("Phase since", f"{_esc(_fmt_dt(self.phase_since))} ({_fmt_dur(self.phase_age_s)} ago)"),
            _row("Trading date", _esc(_date_iso(self.trading_date) or "—")),
            _row(
                "Next session open",
                _esc(_fmt_dt(self.next_session_open))
                + (
                    " <span class='muted'>— event blackout: no entries that day</span>"
                    if self.blackout_next_session
                    else ""
                ),
            ),
            _row("Uptime", f"{_fmt_dur(self.uptime_s)} (since {_esc(_fmt_dt(self.started_at))})"),
            _sep(),
            _row("Kill switch", _badge(ks_cls, ks_txt) + _kill_detail(self)),
            _row("Daily-loss breaker", _badge(brk_cls, brk_txt)),
            # Directly under the breaker: the budget is the rule that makes the
            # breaker's number a bound on the whole day, and the two are read together.
            _row("Loss budget", _loss_budget_cell(self.loss_budget)),
            _row("Audit log", _audit_health(self)),
            # With the kill switch and the breaker, not with the market-data rows:
            # all three answer "what would stop an entry, and is it working?".
            _row("Event blackout", _blackout_cell(self)),
            _sep(),
            _row(
                "Market data",
                _fresh_cell(self.market_data_age_s, self.market_data_max_age_s, self.market_data_stale),
            ),
            _row("Market data link", _link_cell(self.market_data_connected,
                                                self.market_data_reconnects, None)),
            _row(
                "Account data",
                _fresh_cell(self.account_data_age_s, self.account_data_max_age_s, self.account_data_stale),
            ),
            _row("Account link", _link_cell(self.account_connected, self.account_reconnects,
                                            self.account_refresh_consecutive_failures)),
            _row("Account NLV", _esc(_fmt_money(self.account_nlv))),
            _row(
                "Day P&L",
                _badge("warn", "UNANCHORED")
                + " <span class='warn'>no day anchor or no account state — the loss"
                " breaker's input is a placeholder 0.00 and cannot trip</span>"
                if self.daily_pnl_anchored is False
                else _pnl_cell(self.daily_pnl),
            ),
            _row("Open positions", _esc(_fmt_positions(
                self.open_position_count, self.open_spread_equivalent,
                self.working_open_spread_count))),
            # BETWEEN "Open positions" and the unwatched row, because that block is
            # one escalating story about the same held spreads — what is held, is it
            # watched at all, can its triggers be evaluated, can it be exited — and
            # "is the watcher itself alive?" is the step before ALL of them. Not
            # above HALTED: HALTED is the stronger claim and owns the "changes the
            # meaning of everything below" slot.
            _row("Watchdog", _watchdog_cell(self.watchdog_health)),
            # BEFORE the two evaluability rows, because this block is one escalating
            # story about the same held spreads — what is held, is it watched at all,
            # can its triggers be evaluated, can it be exited — and "on the watch list?"
            # is the step before "evaluable?". Severity agrees: an unmanaged position is
            # strictly worse than a BLIND one, which is at least still scanned, marked
            # and flattened.
            _row("Unwatched positions", _unmanaged_cell(self.unmanaged_positions)),
            _row("Exit triggers", _impaired_cell(self.impaired_positions)),
            # Immediately below the row it succeeds, because they are the same
            # position's before and after: a spent attempt has already been popped
            # out of "Exit triggers", and reading the two apart is what made this
            # state invisible until 2026-08-21.
            _row("Exit attempts", _spent_exit_cell(self.spent_exit_attempts)),
            # Beside the exit rows because it is about the same positions' 15:30
            # flatten — and never red: a deferred position keeps both price triggers.
            _row("Flatten deferral", _flatten_deferral_cell(self.flatten_deferral)),
            # LAST in the block, and the only row here about an ORDER rather than a
            # position — which is also why it is the calmest: an abandoned order is
            # exposure that could still become a position, and P5 re-cancels the
            # opening ones once `unmanaged_order_cancel` is promoted.
            _row("Residual orders", _residual_cell(self.residual_orders)),
            # After the whole escalating block, because it is not part of that story:
            # nothing here is a position at risk. It says whether the running process
            # may take a profit and why a held winner is not being taken.
            _row("Profit target", _profit_target_cell(self.profit_target)),
            _sep(),
            _row(
                "Loop lag",
                f"last {_esc(f'{self.heartbeat_last_lag_s * 1000:.1f}ms')}, "
                f"max {_esc(f'{self.heartbeat_max_lag_s * 1000:.1f}ms')}, "
                f"breaches {self.heartbeat_breach_count}, "
                f"sustained {self.heartbeat_sustained_events}",
            ),
            _row(
                "Tier-2 queue",
                _queue_cell(self.audit_queue_depth, self.audit_queue_alert_depth,
                            self.audit_queue_maxsize, self.audit_dropped),
            ),
            _row("Stream backlogs", _queue_depth_cell(self)),
            _row("Memory (RSS)", _memory_cell(self)),
            _row("GC collections", _gc_cell(self)),
            _row("Last audit seq", str(self.audit_last_seq)),
            _row("would_submit today", _would_submit_cell(self)),
            _row("This session", _session_counts_cell(self)),
        ]
        return _PAGE.format(
            env=_esc(self.environment),
            phase=_esc(self.phase),
            generated=_esc(_fmt_dt(self.generated_at)),
            rows="\n".join(rows),
            recent=_recent_table(self.recent),
        )


# -- assembly -----------------------------------------------------------------


@dataclass(frozen=True)
class _DbStatus:
    """The result of the single read-only DB round trip."""

    would_submit_today: int
    kill_trip: dict[str, Any] | None
    recent: tuple[dict[str, Any], ...]


def _read_status_db(
    conn: sqlite3.Connection, *, since_ts: str, recent_limit: int
) -> _DbStatus:
    """Read everything the status view needs in one pass (one lock, one thread hop).

    Runs inside `AuditLog.read`, so it shares the writer's connection and is
    serialized against writes UNDER THE AUDIT CHAIN LOCK — the same lock
    crash-critical Tier 1 writes take. Every query here MUST stay index-backed and
    bounded so the lock is held only microseconds; a scan would delay safety-
    critical writes (see AuditLog.read's contract). Do NOT add an unbounded query.

    The queries, and why each is cheap:
      - would_submit COUNT: `(category, ts)` index (migration 0003) → bounded to
        TODAY's rows, flat in total history.
      - latest kill trip: `category` index + `ORDER BY seq DESC LIMIT 1` → the
        tiny kill_switch.trip group, O(1)-ish.
      - recent tail: `ORDER BY seq DESC LIMIT ?` → reverse primary-key (rowid)
        scan, O(recent_limit).
    All READ-ONLY.
    """
    would = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE category = ? AND ts >= ?",
        (_WOULD_SUBMIT, since_ts),
    ).fetchone()["n"]
    trip_row = conn.execute(
        "SELECT ts, payload FROM audit_log WHERE category = ? ORDER BY seq DESC LIMIT 1",
        (_KILL_TRIP,),
    ).fetchone()
    kill_trip: dict[str, Any] | None = None
    if trip_row is not None:
        payload = _safe_json(trip_row["payload"])
        kill_trip = {
            "ts": trip_row["ts"],
            "source": payload.get("source") if isinstance(payload, dict) else None,
        }
    rows = conn.execute(
        "SELECT seq, ts, tier, category, payload FROM audit_log ORDER BY seq DESC LIMIT ?",
        (recent_limit,),
    ).fetchall()
    recent = tuple(
        {"seq": r["seq"], "ts": r["ts"], "tier": r["tier"],
         "category": r["category"], "payload": r["payload"]}
        for r in rows
    )
    return _DbStatus(
        would_submit_today=int(would),
        kill_trip=kill_trip,
        recent=recent,
    )


class StatusService:
    """Assembles a `StatusSnapshot` from live state + one read-only DB query.

    Holds process-lifetime collaborators (they outlive every session) and reads
    the session-scoped ones through the injected `SessionStatus`. Constructed once
    by the `Application` and handed to the `ControlServer`.
    """

    def __init__(
        self,
        *,
        environment: str,
        started_at: datetime,
        version: str | None = None,
        # Scalars, not a config object: this service is handed everything it needs
        # by `Application.startup` precisely so it imports no config loader.
        # Defaulted so no existing construction site has to be edited, and `None`
        # is a real reading (see `StatusSnapshot.config_profile`).
        config_profile: str | None = None,
        config_filename: str | None = None,
        config_sha: str | None = None,
        credential_slot_trading: str | None = None,
        credential_slot_marketdata: str | None = None,
        # The account those credentials ADDRESS, lifted off the config beside the
        # slots for the same reason they are scalars: this service imports no
        # config loader. `None` is a real reading — unpinned — and the normal one
        # in cert; live cannot reach it (`config.py` refuses to load unpinned).
        broker_account_number: str | None = None,
        session_status: SessionStatus,
        killswitch: KillSwitch,
        heartbeat: LoopHeartbeat,
        audit: AuditLog,
        market_data_max_age_s: float,
        account_stream_max_age_s: float,
        audit_queue_alert_depth: int,
        audit_queue_maxsize: int,
        next_open: Callable[[], datetime | None],
        now: Callable[[], datetime] | None = None,
        recent_limit: int = 15,
        read_rss: Callable[[], int | None] = read_rss_bytes,
        monotonic: Callable[[], float] = time.monotonic,
        halt_state: Callable[[], Any | None] = lambda: None,
        breaker: Callable[[], DailyLossBreaker | None] = lambda: None,
        blackout: BlackoutRunway | None = None,
    ) -> None:
        self._environment = environment
        self._started_at = started_at
        # Resolved once, here, and then constant for the life of the process — the
        # build cannot change under a running container. Injectable so a test can
        # assert on a stamped value without a git-tagged checkout, and defaulting to
        # the real read so no existing construction site (tests, tools) has to be
        # edited to keep working. `None` means "ask the module"; `"unknown"` is a
        # VALUE a caller may deliberately pass.
        self._version = build_version() if version is None else version
        # Constant for the life of the process for the same reason the version is:
        # config is read once at startup and there is deliberately no hot-reload
        # (risk rails changing under a live position is strictly worse than a
        # scheduled restart), so a re-read here could only ever disagree with what
        # the running objects were built from.
        self._config_profile = config_profile
        self._config_filename = config_filename
        self._config_sha = config_sha
        self._credential_slot_trading = credential_slot_trading
        self._credential_slot_marketdata = credential_slot_marketdata
        self._broker_account_number = broker_account_number
        self._session = session_status
        self._killswitch = killswitch
        self._heartbeat = heartbeat
        self._audit = audit
        self._md_max_age = market_data_max_age_s
        self._acct_max_age = account_stream_max_age_s
        self._audit_alert_depth = audit_queue_alert_depth
        self._audit_maxsize = audit_queue_maxsize
        self._next_open = next_open
        self._now = now or et_now
        # The trailing rates' clock — a DURATION clock, separate from `now` (the
        # wall-clock ET time the page reports). See `TrailingRates`.
        self._monotonic = monotonic
        self._recent_limit = recent_limit
        # Read through a callable rather than held as a value: the halt happens
        # after this service is constructed, and /status must report the CURRENT
        # state. Defaults to "never halted" so every other construction site — the
        # tests, and any future caller — keeps working unchanged.
        self._halt_state = halt_state
        # The LIVE daily-loss rail, behind a callable for the same reason as
        # `halt_state` and with the same "nothing there" default: this service is
        # built once in `startup()`, while the breaker is rebuilt for every trading
        # day inside `_run_session_day`, so a value captured here would be a stale
        # object or no object at all.
        #
        # Deliberately the OBJECT and not a pre-computed bool. Reading `.tripped`
        # off the very instance `PreTradeGate` holds is what makes this page and the
        # gate incapable of disagreeing — a bool-returning callable would let a
        # future caller quietly hand back a log-derived answer again. The
        # Application returns None once the held breaker's trading date is past;
        # see `Application._breaker_for_today`.
        self._breaker = breaker
        # A VALUE, not a callable, unlike `halt_state` and `breaker` above — and the
        # asymmetry is the point rather than an oversight. Those two describe live
        # rails that change while the process runs, so reading them through a lambda
        # is what stops the page reporting a halt that is not in force. This one
        # describes a FILE the process read once at startup: a callable here would
        # imply a freshness it does not have and invite a future caller to re-read it
        # on the event loop from inside `/status`.
        self._blackout = blackout
        # Injected so the reader is testable off Linux (no /proc on a dev Mac).
        self._read_rss = read_rss
        # The baseline for the growth figure, captured HERE — inside
        # `Application.startup()`, before the first session — so `/status` answers
        # "how much has this process grown" from a single poll. Captured once and
        # never refreshed: a moving baseline would hide exactly the trend it exists
        # to expose.
        self._rss_at_start = read_rss()
        # The per-hour rates' history. Deliberately NOT seeded here: this moment is
        # the start of the startup cost the rates must not report, so the window's
        # earliest possible anchor is the first sampler tick, not construction.
        self._rates = TrailingRates()
        self._sampler: asyncio.Task[None] | None = None

    def sample(self) -> None:
        """Record one `(rss, gen-2)` point for the trailing rates. Two O(1) reads,
        safe on the event loop (see `read_rss_bytes`, `gc_snapshot`)."""
        _, collections = gc_snapshot()
        self._rates.add(self._monotonic(), self._read_rss(), collections[2])

    def start_sampling(self, interval_s: float = RATE_SAMPLE_INTERVAL_S) -> None:
        """Feed the trailing rates on a fixed cadence, independent of polling — a
        rate derived only from poll times would depend on who happened to look."""
        if self._sampler is None or self._sampler.done():
            self._sampler = asyncio.create_task(
                self._sample_forever(interval_s), name="status-rate-sampler"
            )

    async def stop_sampling(self) -> None:
        if self._sampler is not None:
            self._sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sampler
            self._sampler = None

    async def _sample_forever(self, interval_s: float) -> None:
        """Sample until cancelled — and SURVIVE a failed sample.

        A sampler task that died on one exception used to end silently: the rates
        kept being computed against an ever-older anchor, so the page showed a
        number with no sign it had stopped being a trailing hour (only
        `rate_window_s` creeping past 3,660 gave it away). A failure now costs that
        one sample and is logged — the first one with its traceback, then one line
        per `_SAMPLER_LOG_EVERY` failures so a persistent defect cannot flood the
        journal. Nothing here is a rail, so nothing pages.
        """
        failures = 0
        while True:
            await asyncio.sleep(interval_s)
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - a diagnostic must not die on one read
                failures += 1
                if failures == 1:
                    logger.exception("status rate sampler: sample failed; continuing")
                elif failures % _SAMPLER_LOG_EVERY == 0:
                    logger.warning(
                        "status rate sampler: %d samples failed so far; continuing",
                        failures,
                    )

    async def assemble(self) -> StatusSnapshot:
        now = self._now()
        db = await self._audit.read(
            lambda conn: _read_status_db(
                conn, since_ts=_et_day_start_utc_iso(now), recent_limit=self._recent_limit
            )
        )

        gc_counts, gc_collections = gc_snapshot()
        rss_now = self._read_rss()
        rate = self._rates.rate(self._monotonic(), rss_now, gc_collections[2])
        risk = self._session.current_risk()
        links = self._session.current_links()
        md_age = risk.market_data_age_s if risk is not None else None
        acct_age = risk.account_stream_age_s if risk is not None else None
        ks = self._killswitch
        hb = self._heartbeat
        # The trip row is the newest `kill_switch.trip` in the DB, and it outlives
        # the process that wrote it. So `source`/`since` describe the CURRENT trip
        # only while one is engaged; otherwise they belong to a halt that has since
        # been RESUMED, and reporting them alongside `engaged: false` reads as a
        # live halt that is not happening. Matched to how `halted` already behaves:
        # its source and since come off the live halt object and are None when
        # nothing is halted (the HTML view had this right — `_kill_detail` returns
        # "" unless engaged — the JSON did not).
        #
        # **This gate is what made the page misreport a restart-while-halted, and
        # it is now CORRECT rather than removed** (2026-09-01). The defect was
        # never the gate: it was that `ks.engaged` was false while a halt was in
        # force, so the page hid a row it should have shown. Rehydration fixes that
        # at the source — the object is now BUILT from this very row at startup, so
        # the two can no longer disagree. Deleting the gate instead would have
        # introduced the mirror bug this comment has always warned about: a resumed
        # trip rendering as a live one.
        trip = db.kill_trip if ks.engaged else None
        halt = self._halt_state()
        # The live rail itself. None here is "no breaker armed for today", which the
        # snapshot carries through as None rather than flattening to False.
        breaker = self._breaker()

        return StatusSnapshot(
            generated_at=now,
            environment=self._environment,
            version=self._version,
            config_profile=self._config_profile,
            config_filename=self._config_filename,
            config_sha=self._config_sha,
            credential_slot_trading=self._credential_slot_trading,
            credential_slot_marketdata=self._credential_slot_marketdata,
            broker_account_number=self._broker_account_number,
            started_at=self._started_at,
            uptime_s=max(0.0, (now - self._started_at).total_seconds()),
            phase=self._session.phase.value,
            phase_since=self._session.phase_since,
            phase_age_s=max(0.0, (now - self._session.phase_since).total_seconds()),
            trading_date=self._session.trading_date,
            next_session_open=self._next_open(),
            market_data_age_s=md_age,
            market_data_max_age_s=self._md_max_age,
            market_data_stale=(None if md_age is None else is_stale(md_age, self._md_max_age)),
            account_data_age_s=acct_age,
            account_data_max_age_s=self._acct_max_age,
            account_data_stale=(None if acct_age is None else is_stale(acct_age, self._acct_max_age)),
            market_data_connected=(links.market_data_connected if links else None),
            market_data_reconnects=(links.market_data_reconnects if links else None),
            account_connected=(links.account_connected if links else None),
            account_reconnects=(links.account_reconnects if links else None),
            account_refresh_consecutive_failures=(
                links.account_refresh_consecutive_failures if links else None
            ),
            market_data_queue_depths=(links.market_data_queue_depths if links else {}),
            account_queue_depths=(links.account_queue_depths if links else {}),
            account_nlv=(risk.account_nlv if risk is not None else None),
            daily_pnl=(risk.daily_pnl if risk is not None else None),
            daily_pnl_anchored=(risk.daily_pnl_anchored if risk is not None else None),
            loss_budget=(risk.loss_budget if risk is not None else None),
            open_position_count=(risk.open_position_count if risk is not None else None),
            open_spread_equivalent=(risk.open_spread_equivalent if risk is not None else None),
            working_open_spread_count=(
                risk.working_open_spread_count if risk is not None else None
            ),
            impaired_positions=self._session.current_evaluability(),
            spent_exit_attempts=self._session.current_spent_exits(),
            watchdog_health=self._session.current_watchdog_health(),
            profit_target=self._session.current_profit_target(),
            flatten_deferral=self._session.current_flatten_deferral(),
            unmanaged_positions=self._session.current_unmanaged(),
            residual_orders=self._session.current_residuals(),
            entries_ended=self._session.current_entries_ended(),
            heartbeat_last_lag_s=hb.last_lag,
            heartbeat_max_lag_s=hb.max_lag,
            heartbeat_breach_count=hb.breach_count,
            heartbeat_sustained_events=hb.sustained_events,
            rss_bytes=rss_now,
            rss_at_start_bytes=self._rss_at_start,
            gc_counts=gc_counts,
            gc_collections=gc_collections,
            rss_growth_per_hour=(rate.rss_bytes_per_hour if rate else None),
            gc_gen2_per_hour=(rate.gen2_per_hour if rate else None),
            rate_window_s=(rate.window_s if rate else None),
            audit_last_seq=self._audit.last_seq,
            audit_queue_depth=self._audit.queue_depth,
            audit_queue_alert_depth=self._audit_alert_depth,
            audit_queue_maxsize=self._audit_maxsize,
            audit_dropped=self._audit.dropped,
            audit_failed=self._audit.failed,
            killswitch_engaged=ks.engaged,
            killswitch_reason=ks.reason,
            killswitch_source=(trip.get("source") if trip else None),
            killswitch_since=(trip.get("ts") if trip else None),
            # Off the live switch, not the row: "was this halt inherited?" is a
            # question about the object, and the row cannot answer it — the same
            # row is the source of `since` in both cases.
            killswitch_restored=ks.restored,
            breaker_tripped=(None if breaker is None else breaker.tripped),
            halted=halt is not None,
            halt_reason=(halt.reason if halt else None),
            halt_source=(halt.source if halt else None),
            halted_since=(halt.at.isoformat() if halt else None),
            # `str()` rather than the enum: this snapshot is serialized to JSON and
            # rendered to HTML, and neither wants a `HaltGrade`. `getattr` because
            # `halt_state` is `Any`-typed by design (status.py deliberately does not
            # import `app`), so a test double predating the field reads as None
            # instead of raising — the same tolerance the field's own default has.
            halt_grade=(str(getattr(halt, "grade", None)) if halt else None),
            blackout=self._blackout,
            would_submit_today_raw=db.would_submit_today,
            session_counts=self._session_counts(),
            recent=db.recent,
        )

    def _session_counts(self) -> dict[str, int]:
        """Categories that moved since this session's loop came up.

        `AuditLog.counts` is an IN-MEMORY tally (seeded once at construction,
        incremented in `_advance`), so this costs O(categories) and issues no query.
        That matters: the obvious alternative — a `GROUP BY category` over today's
        rows — would run through `AuditLog.read`, which holds the chain lock, and
        `read`'s own docstring forbids exactly that. It is also the mistake that
        made the journal's status line a growing event-loop stall.

        Returns {} when no baseline was published (no session running), rather than
        differencing against zero and reporting every all-time total as if it had
        just happened.
        """
        baseline = self._session.counts_baseline
        if baseline is None:
            return {}
        now = self._audit.counts
        return {
            category: total - baseline.get(category, 0)
            for category, total in now.items()
            if total - baseline.get(category, 0) > 0
        }


# -- small pure helpers -------------------------------------------------------


def _et_day_start_utc_iso(now_et: datetime) -> str:
    """Start of the current ET calendar day, as the UTC `ts` string to compare.

    "Today's" would_submit count is scoped to the ET trading date. Sessions run
    entirely within one ET day, so a `ts >= <ET-midnight-in-UTC>` filter captures
    exactly the current day's records. Only the LOWER bound is used here — these
    are "today, so far" counts, and nothing can be timestamped later than now.
    """
    return et_day_bounds_utc_iso(now_et.date())[0]


def _num_or_none(x: float | None) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(x, 4)


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {"_raw": raw}


def _date_iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _dt_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# -- HTML rendering helpers (all output escaped) ------------------------------

_PHASE_CLASS = {
    "running": "ok",
    "logging_in": "warn",
    "reconciling": "warn",
    "idle": "muted",
    "ended": "muted",
    "aborted": "bad",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _badge(cls: str, text: str) -> str:
    return f"<span class='badge {cls}'>{_esc(text)}</span>"


def _row(label: str, value_html: str) -> str:
    # `label` is a fixed literal from this module; `value_html` is pre-escaped/
    # pre-built by the caller.
    return f"<tr><th>{_esc(label)}</th><td>{value_html}</td></tr>"


def _sep() -> str:
    return "<tr class='sep'><td colspan='2'></td></tr>"


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _fmt_dur(seconds: float) -> str:
    s = int(max(0.0, seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def _fmt_money(x: float | None) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"${x:,.2f}"


def _fmt_int(x: int | None) -> str:
    return "—" if x is None else str(x)


def _impaired_cell(positions: tuple[PositionEvaluability, ...] | None) -> str:
    """The exit-triggers row. Three states, and they must not look alike.

    No watchdog publishing renders muted ("not applicable"), an empty tuple renders
    OK ("all evaluable"), and anything impaired renders per position with its reasons
    and how long it has been that way — the duration being the difference between a
    momentary quote gap and something worth going to look at.
    """
    if positions is None:
        return "<span class='muted'>— (no watchdog)</span>"
    if not positions:
        return "<span class='ok'>all evaluable</span>"
    cells = []
    for p in positions:
        # `blind` is the paging severity: no stop that can FIRE is evaluable
        # (`watchdog.TriggerThresholds.acting_stops`, 2026-09-27). Degraded keeps a
        # live stop and is deliberately not styled as an emergency. The time-based
        # flatten is excluded from that measure on purpose — it can never be
        # unevaluable, so counting it would make `blind` unreachable.
        cls = "bad" if p.blind else "warn"
        label = "BLIND" if p.blind else "degraded"
        cells.append(
            f"<span class='{cls}'>{_esc(p.candidate_id)}: {_esc(label)} "
            f"({_esc(', '.join(p.unknown_reasons))}) for "
            f"{_esc(_fmt_dur(p.impaired_for_s))}</span>"
        )
    return "<br>".join(cells)


def _watchdog_cell(health: WatchdogHealth | None) -> str:
    """The Watchdog row. Three states, and the third is why the row exists.

    A watchdog that parks the process is loud — the process is gone and the
    supervisor restarts it. A watchdog that keeps running while failing every tick
    is, by construction, the quietest failure this system can produce, which is why
    this row is not optional (§3.9, 2026-09-10): the HALTED row's "NO ENTRIES —
    watchdog still running" is now the one sentence on this page that may be false,
    and this is the row that says which way it fell.

    Severity follows the states' meanings: DEGRADED is red — something is failing
    right now and the rows below are as of the last clean pass; RECOVERED is amber —
    watching again, but entries stay halted until `POST /resume`, because the halt is
    permanent and the failure is not; WATCHING is green. The words carry the state
    as well as the colour, because a colour does not survive a phone screenshot
    pasted into Slack.
    """
    if health is None:
        return "<span class='muted'>— (no watchdog)</span>"
    if health.degraded:
        clean = (
            "never"
            if health.last_clean_pass_s is None
            else f"{_fmt_dur(health.last_clean_pass_s)} ago"
        )
        affected = (
            f"; affected: {_esc(', '.join(health.affected))}"
            if health.affected
            else ""
        )
        return (
            _badge("bad", "DEGRADED")
            + f" <span class='bad'>{_esc(health.where)} — {_esc(health.error_type)}:"
            f" {_esc(health.error or '')}</span>"
            + f"<br><span class='bad'>&nbsp;&nbsp;failing for"
            f" {_esc(_fmt_dur(health.degraded_for_s or 0.0))},"
            f" last clean pass {_esc(clean)}{affected}</span>"
            + "<br><span class='muted'>&nbsp;&nbsp;the rows below are as of the"
            " last clean pass</span>"
        )
    if health.failures:
        return (
            _badge("warn", "RECOVERED")
            + f" <span class='warn'>{health.failures} failure(s),"
            f" last {_esc(health.error_type or '?')}"
            f" {_esc(_fmt_dur(health.last_failure_for_s or 0.0))} ago</span>"
            + "<br><span class='muted'>&nbsp;&nbsp;entries remain halted —"
            " POST /resume once resolved</span>"
        )
    if health.last_clean_pass_s is None:
        return _badge("ok", "WATCHING") + " <span class='ok'>no pass completed yet</span>"
    return (
        _badge("ok", "WATCHING")
        + f" <span class='ok'>{health.watching} position(s), last pass"
        f" {_esc(_fmt_dur(health.last_clean_pass_s))} ago</span>"
    )


def _flatten_deferral_cell(state: FlattenDeferralStatus | None) -> str:
    """The flatten-deferral row. (2026-09-25)

    The loaded mode and parameters first, because a change is a config-only release
    and this is where it is confirmed live. Then one line per HELD position the
    deferral decided on: green while deferring (both price triggers stay live), muted
    once released to the exit path. Never red — a deferral never weakens a stop.
    """
    if state is None:
        return "<span class='muted'>— (no watchdog)</span>"
    params = (
        f"k {state.k:g}, z99 {state.tail_z99:g}, alpha {state.tail_alpha:g}"
    )
    if state.mode == "off":
        head = f"<span class='muted'>off — the 15:30 flatten always fires ({params})</span>"
    elif state.mode == "record":
        head = (
            "<span class='warn'>RECORD — decided and recorded, the flatten always "
            f"fires</span> <span class='muted'>({params})</span>"
        )
    else:
        head = _badge("ok", "ON") + f" <span class='muted'>({params})</span>"
    cells = [head]
    for r in state.rows:
        z = "?" if r.z is None else f"{r.z:.2f}"
        p = "?" if r.p_touch is None else f"{r.p_touch:.1e}"
        lhs = "?" if r.lhs is None else f"{r.lhs:.3f}"
        if r.deferring:
            cells.append(
                f"<span class='ok'>&nbsp;&nbsp;{_esc(r.candidate_id)}: flatten DEFERRED "
                f"for {_esc(_fmt_dur(r.deferred_for_s))} — z {z}, P(touch) {p}, "
                f"k·P·W {lhs} ≤ {r.premium:.2f}; its stop stays live to the close</span>"
            )
        else:
            cells.append(
                f"<span class='muted'>&nbsp;&nbsp;{_esc(r.candidate_id)}: flatten "
                f"released ({_esc(r.reason)}) — z {z}</span>"
            )
    return "<br>".join(cells)


def _profit_target_cell(state: ProfitTargetStatus | None) -> str:
    """The profit-target row. (2026-09-23)

    The switch first, in words, because it is what a promotion or a rollback changes
    and the page is the only place that change can be confirmed live. Then one line
    per HELD position the target has left, muted when it simply ran out of looks and
    amber in the one state that needs a human: its order was never confirmed terminal,
    so a close may be resting at the broker. Never red — every emergency protection is
    intact for every position on this row, by construction.
    """
    if state is None:
        return "<span class='muted'>— (no watchdog)</span>"
    if not state.enabled:
        head = (
            "<span class='muted'>off — evaluated and recorded, nothing sent"
            " (watchdog.profit_target_enabled = false)</span>"
        )
    else:
        head = (
            _badge("ok", "ON")
            + f" <span class='ok'>{state.taken} taken this session</span>"
        )
    cells = [head]
    for r in state.retired:
        if r.outcome in ("filled", "filled_late"):
            # The take CLOSED it. On a full fill the ledger catches up in seconds and
            # the row goes. A PARTIAL fill leaves a stub whose automated close is spent
            # (the guard blocks on the take's fill), and the Exit attempts row above
            # says so in red — so this row must not claim the stop is intact, and
            # names a still-possible working remainder in amber (2026-09-23 review).
            cls = "warn" if r.order_may_be_working else "muted"
            working = (
                "; its remainder cancel was not confirmed — check the broker"
                if r.order_may_be_working
                else ""
            )
            cells.append(
                f"<span class='{cls}'>{_esc(r.candidate_id)}: profit taken"
                f" ({_esc(r.outcome)}) {_esc(_fmt_dur(r.retired_for_s))} ago — if"
                f" still held, see Exit attempts{working}</span>"
            )
        elif r.order_may_be_working:
            cells.append(
                f"<span class='warn'>{_esc(r.candidate_id)}: retired UNCONFIRMED"
                f" ({_esc(r.outcome)}) {_esc(_fmt_dur(r.retired_for_s))} ago — check"
                " the broker for a working close; the emergency exit is intact</span>"
            )
        else:
            cells.append(
                f"<span class='muted'>{_esc(r.candidate_id)}: retired"
                f" ({_esc(r.outcome)}) {_esc(_fmt_dur(r.retired_for_s))} ago"
                " — nothing at the broker; the emergency exit is intact</span>"
            )
    return "<br>".join(cells)


def _spent_exit_cell(positions: tuple[UnclosablePosition, ...] | None) -> str:
    """The exit-attempts row. Three states again, and only one of them is calm.

    Mirrors `_impaired_cell`'s no-watchdog / nothing-wrong / something-wrong split
    so the two adjacent rows read the same way — but with NO severity gradient in
    the third state. `_impaired_cell` styles `degraded` amber because the underlying
    backstop is still live and it is genuinely not an emergency. There is no
    equivalent here: every entry means the stop already fired, the position is still
    held, no trigger is being evaluated for it, nothing automated will try again,
    and it expires today. All of them are red, and all of them say so in words —
    a colour alone does not survive a phone screenshot pasted into Slack.

    The outcome is shown verbatim because it is the operator's next move: a walk
    that timed out leaves an order that may still fill (so the first question is
    what the broker shows), while a refused cross leaves nothing working at all.

    **The mark rides on the same line** (2026-08-28). Whoever reads this row is
    deciding whether to close the spread by hand, and that decision is made on what
    it costs now against the stop that fired — not on the fact that it fired. It is
    a second line rather than more words on the first because this row is read on a
    phone. `unpriced` is rendered where the mark is missing, never a blank: a row
    that silently drops the numbers reads as a row with nothing to say.
    """
    if positions is None:
        return "<span class='muted'>— (no watchdog)</span>"
    if not positions:
        return "<span class='ok'>all intact</span>"
    cells = []
    for p in positions:
        cells.append(
            f"<span class='bad'>{_esc(p.candidate_id)}: NO EXIT LEFT "
            f"({_esc(p.outcome)}, {p.contracts}x) for "
            f"{_esc(_fmt_dur(p.spent_for_s))} — needs a human</span>"
            # A close STILL WORKING at the broker (2026-09-25). Its own line, and the
            # instruction is the point: on 2026-09-24 this row read "no exit left" for
            # 22 minutes while a crossing limit was resting, which invites a manual
            # second close on top of it (C5's double close).
            + (
                f"<br><span class='warn'>&nbsp;&nbsp;CLOSE WORKING at the broker: "
                f"#{p.working_close_order_id}"
                + (
                    f" at {p.working_close_limit:.2f}"
                    if p.working_close_limit is not None
                    else ""
                )
                + (
                    f" for {_esc(_fmt_dur(p.working_close_for_s))}"
                    if p.working_close_for_s is not None
                    else ""
                )
                + " — check the broker BEFORE sending anything by hand</span>"
                if p.working_close_order_id is not None
                else ""
            )
            # The crash sentence, when a crash is what spent the attempt
            # (2026-09-10). Its own line and NOT muted: `not_resolved` with no
            # broker reason anywhere is the reassuring-when-wrong reading this row
            # has been fixed for twice already, and this is the line that says so.
            + (
                f"<br><span class='bad'>&nbsp;&nbsp;{_esc(p.crash_error)}</span>"
                if p.crash_error
                else ""
            )
            # The stub-close allowance (2026-09-24). On its own muted line under
            # the headline: the headline predates the continuation and stays
            # true for the operator's purposes ("nothing ELSE will try"), while
            # this says whether the ONE further close is still coming — the
            # difference between watching the next tick and picking up the
            # phone.
            + (
                f"<br><span class='muted'>&nbsp;&nbsp;STUB CLOSE: "
                f"{_esc(p.stub_close)}</span>"
                if p.stub_close
                else ""
            )
            + f"<br><span class='muted'>&nbsp;&nbsp;{_esc(_fmt_spent_mark(p))}</span>"
        )
    return "<br>".join(cells)


def _unmanaged_cell(positions: tuple[UnmanagedPosition, ...] | None) -> str:
    """The unwatched-positions row. Three states, and the third one is the worst
    thing this document can say.

    Mirrors `_impaired_cell` / `_spent_exit_cell`'s no-watchdog / nothing-wrong /
    something-wrong split so the three adjacent rows read alike. The severity model is
    `_impaired_cell`'s, not `_spent_exit_cell`'s: there IS a milder state here, and it
    is `settling` — a malformed-only condition inside `watchdog.book_settle_s`, i.e.
    the ordinary window where the two legs of one multi-leg order book ~0.25s apart and
    a spread legitimately has one applied leg. `_report_unwatchable` suppresses its own
    page for exactly that, and colouring it red would be the same false positive the
    link row was written to avoid.

    Everything else is red AND says so in words. A colour does not survive a phone
    screenshot pasted into Slack, and the words are the part that is not obvious: this
    row is the only place that says such a position has **no stop, no mark and no
    flatten** — a reader who knows the watchdog is running will otherwise assume all
    three, because for every other position on this page they hold.

    Bounded by COUNT, never by characters. `problem` is `store.py`'s own sentence and
    is the operator's whole diagnosis, so it renders verbatim — reintroducing the audit
    tail's 240-char cut here would defeat the reason live rows exist at all.
    """
    if positions is None:
        return "<span class='muted'>— (no watchdog)</span>"
    if not positions:
        return "<span class='ok'>all explained</span>"
    cells = []
    for p in positions[:MAX_UNMANAGED_ROWS]:
        cls = "warn" if p.settling else "bad"
        label = "settling" if p.settling else "NOT WATCHED"
        # Whatever names it best: a trade idea if the ledger knew one, else the order,
        # else the bare symbol. An `unattributed` row has only the last of the three.
        name = p.candidate_id or p.external_identifier or (
            p.symbols[0] if p.symbols else p.kind
        )
        head = (
            f"<span class='{cls}'>{_esc(name)}: {_esc(label)} ({_esc(p.kind)}) "
            f"for {_esc(_fmt_dur(p.unwatchable_for_s))}</span>"
        )
        if not p.settling:
            head += (
                " <span class='bad'>— no stop, no mark, NO 15:30 flatten;"
                " needs a human</span>"
            )
        lines = [head, f"<span class='muted'>&nbsp;&nbsp;{_esc(p.problem)}</span>"]
        detail = _unmanaged_detail_text(p)
        # Suppressed when it would only repeat the head. An `unattributed` row is
        # NAMED by its symbol — there is no candidate and no order — so printing the
        # symbol list under it is one wasted line on a phone.
        if detail and detail != name:
            lines.append(f"<span class='muted'>&nbsp;&nbsp;{_esc(detail)}</span>")
        cells.append("<br>".join(lines))
    if len(positions) > MAX_UNMANAGED_ROWS:
        cells.append(
            f"<span class='bad'>+{len(positions) - MAX_UNMANAGED_ROWS} more —"
            " see watchdog.unwatchable in the audit tail</span>"
        )
    return "<br>".join(cells)


def _unmanaged_detail_text(p: UnmanagedPosition) -> str:
    """The per-symbol half of one unmanaged row. Plain text — the caller escapes it.

    `detail` wins over `symbols` where both exist, because it carries the same symbols
    plus the numbers that ARE the diagnosis ("the ledger says 2 and `positions` says
    1"). Capped by symbol count so a pathological ledger cannot produce a row that has
    to be scrolled past; a lopsided remainder has one to three symbols in practice.
    """
    if p.detail:
        shown = p.detail[:MAX_UNMANAGED_SYMBOLS]
        text = "; ".join(f"{sym}: {note}" for sym, note in shown)
        extra = len(p.detail) - len(shown)
    elif p.symbols:
        shown = p.symbols[:MAX_UNMANAGED_SYMBOLS]
        text = ", ".join(shown)
        extra = len(p.symbols) - len(shown)
    else:
        return ""
    return text + (f" (+{extra} more)" if extra else "")


def _residual_cell(orders: tuple[ResidualOrderRow, ...] | None) -> str:
    """The residual-orders row: what a finished walk left working at the broker.

    Three states again. The third is AMBER rather than red, and that is a judgment
    about what is actionable: an abandoned order is exposure that could still become a
    position, not a position with no stop, and P5's recheck is already asking the
    broker about it every poll. It escalates to RED on one condition — `last_sighting`
    being a read gap (`absent` / `unreadable` / `ambiguous`) — because that is the
    state P5 treats as no verdict in either direction: the broker will neither confirm
    the order dead nor license a cancel, so nothing is progressing.

    **The mode is on the summary line, once.** It is a property of the recheck, not of
    a residual, and repeating it per row would read as if it could differ between them.
    Saying it at all matters more while `unmanaged_order_cancel = false`, not less: the
    flip is a config change with no deploy, so this is the only place an operator can
    confirm which mode is actually live.
    """
    if orders is None:
        return "<span class='muted'>— (no watchdog)</span>"
    if not orders:
        return "<span class='ok'>none working</span>"
    enabled = orders[0].cancel_enabled
    summary = (
        f"<span class='warn'>{len(orders)} working</span> "
        + (
            "<span class='muted'>— auto-cancel ON for opening orders</span>"
            if enabled
            else "<span class='warn'>— OBSERVER MODE (would_cancel): nothing is"
            " being cancelled</span>"
        )
    )
    cells = [summary]
    for r in orders[:MAX_UNMANAGED_ROWS]:
        gap = r.last_sighting in _RESIDUAL_READ_GAPS
        cls = "bad" if gap else "warn"
        name = r.candidate_id or r.external_identifier
        bits = [
            f"order {r.broker_order_id} ({_esc(r.minted_direction)})"
            # The resting market-crossing close (2026-09-25): working is its job.
            + (" — resting cross" if r.working_expected else "")
            + (f" at {r.limit_price:.2f}" if r.limit_price is not None else ""),
            f"working {_esc(_fmt_dur(r.working_for_s))}",
            f"{r.attempts} cancel attempt{'' if r.attempts == 1 else 's'}",
            f"last {_esc(r.last_status or '?')}/{_esc(r.last_sighting or '?')}",
        ]
        line = (
            f"<span class='{cls}'>&nbsp;&nbsp;{_esc(name)}: " + ", ".join(bits) + "</span>"
        )
        if gap:
            # The sentence P5's own rule states. Without it a red row reads as "the
            # cancel failed", which is a different and more tractable problem.
            line += (
                " <span class='bad'>— the broker will neither confirm it dead nor"
                " licence a cancel</span>"
            )
        elif r.minted_direction != "open":
            # `HaltCheckFn`'s asymmetry, verbatim: cancelling a close strands a
            # position, so this one is watched and paged and never auto-cancelled —
            # in EITHER mode, which the summary line above does not say.
            line += (
                " <span class='muted'>— closing order: never auto-cancelled</span>"
            )
        cells.append(line)
    if len(orders) > MAX_UNMANAGED_ROWS:
        cells.append(
            f"<span class='warn'>&nbsp;&nbsp;+{len(orders) - MAX_UNMANAGED_ROWS} more"
            " — see order.residual_* in the audit tail</span>"
        )
    return "<br>".join(cells)


#: The three `ResidualSighting` values that are read gaps rather than states. As
#: STRINGS, because that is what crosses the seam — `status.py` imports nothing from
#: `execution/`. A test pins them against the enum so a rename cannot silently turn
#: the red state off.
_RESIDUAL_READ_GAPS = frozenset({"absent", "unreadable", "ambiguous"})


def _fmt_spent_mark(p: UnclosablePosition) -> str:
    """The mark line under a spent-exit row. Plain text — the caller escapes it.

    Three states, and the middle one is the point: `still_triggered=False` means the
    stop that fired has come back inside its threshold, which changes what a human
    should do and is invisible from `outcome` alone.
    """
    if p.cost_to_close is None:
        return "mark: unpriced"
    if p.credit_trigger_enabled:
        stop = "?" if p.credit_stop is None else f"{p.credit_stop:.2f}"
        head = f"mark: {p.cost_to_close:.2f} vs stop {stop}"
    else:
        # No credit stop exists to compare against (2026-09-27): the stop is the
        # strike touch, and `still_triggered` below already says where it stands.
        head = f"mark: {p.cost_to_close:.2f} (credit trigger off)"
    if p.still_triggered is None:
        state = "not yet evaluated"
    elif p.still_triggered:
        state = "STILL THROUGH THE STOP"
    else:
        state = "back inside the stop"
    return f"{head} — {state}"


def _fmt_positions(
    legs: int | None, spreads: int | None, working: int | None = None
) -> str:
    """Both units, always. The broker counts LEGS and every risk cap is stated in
    SPREADS, so a bare "2" on the phone is ambiguous in exactly the way that put a
    leg count on the wrong side of `max_concurrent_positions` until 2026-08-13.

    A non-zero `working` count is appended: those are OPENING orders not yet filled
    that the concurrency cap still counts, so `spreads` can exceed the leg count
    and this is what explains it (Finding 6, 2026-08-28)."""
    if legs is None:
        return "—"
    if spreads is None:
        return str(legs)
    text = f"{legs} leg{'' if legs == 1 else 's'} ({spreads} spread{'' if spreads == 1 else 's'})"
    if working:
        text += f", +{working} working"
    return text


def _blackout_cell(snap: StatusSnapshot) -> str:
    """The macro-event blackout list's remaining runway.

    **Silent in the common case**, which is the whole design constraint: for eleven
    months of a twelve-month list this row is one plain sentence with no colour, and a
    permanent amber banner would train the eye past the row on the one month it
    matters. Colour appears only when there is something to do.

    Five readings:

      - `ok` — the count and the horizon, plain. Enough to sanity-check that the file
        the process actually loaded is the one that was deployed.
      - `expiring` — AMBER, with the date, the days left and the command that fixes
        it. Named `warn` rather than `bad` because nothing is broken yet.
      - `exhausted` — RED. The list has run out (or holds no dates at all) and Argus
        is now trading FOMC/CPI/NFP/PCE days as ordinary days. This is the state the
        whole field exists to make visible, because from inside the process it is
        indistinguishable from a quiet calendar.
      - `unreadable` — RED, and doubly so: `[events] enabled = true` against an
        unreadable file is a hard `ConfigError` at SESSION start, so this also
        predicts that today's session will abort.
      - `disabled` — MUTED, and spelled out. Not a warning (it is a deliberate config
        choice) but it is the only surface that says the stand-down is off at all.

    On a blackout day an `ok` or `expiring` reading is prefixed with a grey `IN FORCE
    TODAY` badge — the one place the row used to read calm while the thing it names
    was actually suppressing every entry.
    """
    b = snap.blackout
    if b is None:
        return "<span class='muted'>—</span>"
    state = snap.blackout_state
    if state == "disabled":
        return (
            "<span class='muted'>off — entries are NOT suppressed on macro-event "
            "days ([events] enabled = false)</span>"
        )
    if state == "unreadable":
        return (
            _badge("bad", "UNREADABLE")
            + f' <span class="muted">{_esc(b.error or "")}</span>'
            + " <span class='bad'>— every session will abort at start</span>"
        )
    if state == "exhausted":
        through = "no dates at all" if b.last_date is None else (
            f"ran out {_esc(_date_iso(b.last_date))}"
        )
        return (
            _badge("bad", "EXHAUSTED")
            + f" <span class='bad'>{through} — FOMC/CPI/NFP/PCE days are being"
            " traded as ordinary days</span>"
            + _BLACKOUT_FIX
        )
    days = snap.blackout_days_remaining
    body = (
        f"{b.date_count} date{'' if b.date_count == 1 else 's'}, through "
        f"{_esc(_date_iso(b.last_date))} ({days} day{'' if days == 1 else 's'})"
    )
    # Whether the list is covering TODAY, ahead of what is left of it. Only in the two
    # states where the list is in force and readable — `exhausted` can cover nothing,
    # and the other three returned above.
    today = _badge("muted", "IN FORCE TODAY") + " " if snap.blackout_today else ""
    if state == "expiring":
        return (
            today + _badge("warn", "EXPIRING") + f" <span class='warn'>{body}</span>"
            + _BLACKOUT_FIX
        )
    return today + f"<span class='muted'>{body}</span>"


#: The remediation, spelled out on both loud states. An operator reading this on a
#: phone at 15:00 should not have to go and find the tool's name.
_BLACKOUT_FIX = (
    " <span class='muted'>— regenerate with "
    "<code>python -m argus.tools.blackout_dates</code> (needs next year's FRED "
    "releases and a hand-verified fomc_dates.txt), then redeploy the config</span>"
)


def _version_cell(version: str) -> str:
    """The running build's git description.

    Three readings, and the colours follow what is ACTIONABLE rather than what is
    unusual — the rule `_link_cell` states and this row is the second place it
    binds:

      - a clean stamped version is plain text. It is the expected state and colouring
        it green would put a badge on the page that never means anything.
      - `unknown` is MUTED with the reason spelled out. It is not a fault: it is what
        an ad hoc build reports, and the fix (pass the build arg) belongs in the
        deploy, not in an alert.
      - `-dirty` is AMBER and says the word. It is the "what is running is not what I
        think" signal this whole field exists for — but it is deliberately not red,
        because the droplet's config file is tracked and edited in place, so a dirty
        build tree is an ordinary condition there. Red on a condition that is usually
        benign is how a red badge stops meaning anything (2026-08-05).
    """
    if version == UNKNOWN:
        return (
            "<span class='muted'>unknown</span> <span class='muted'>&mdash; built "
            "without ARGUS_VERSION; see the runbook's build step</span>"
        )
    if is_dirty(version):
        return (
            f"<strong>{_esc(version)}</strong> "
            "<span class='warn'>&mdash; built from a tree with uncommitted changes "
            "to tracked files</span>"
        )
    return f"<strong>{_esc(version)}</strong>"


def _config_cell(profile: str | None, filename: str | None, sha: str | None) -> str:
    """Which config file is loaded, and the fingerprint of its bytes.

    Same colour rule as `_version_cell`: ACTIONABLE, not unusual. The expected
    state — a named profile and a sha — is plain text, because this row is read on
    every deploy and a badge that is always lit is a badge that means nothing.

    Two loud states, and only one of them is a fault:

      - no file behind the config at all (`from_dict`) is MUTED. It is what a tool
        or a test reports and it cannot happen to a deployed process.
      - a file whose name carries NO PROFILE is AMBER. It is legal — an ad hoc
        `--config /tmp/x.toml` — but it is also the state in which `_check_profile`
        asserts NOTHING, so the one rail standing between "the file says live" and
        "the file is named live" is not armed. Since the rename it should never be
        seen on the droplet at all: compose mounts `argus.${ARGUS_ENV}.toml`, so a
        profile-less name there means something is pointing at a file by path.
        Worth a second look; hence amber, and hence saying why.
    """
    if filename is None:
        return (
            "<span class='muted'>—</span> <span class='muted'>&mdash; built without "
            "a file (Config.from_dict); not a deployed state</span>"
        )
    body = f"<strong>{_esc(filename)}</strong> <code>@{_esc(sha or '—')}</code>"
    if profile is None:
        return (
            f"{body} <span class='warn'>&mdash; no profile in the filename, so the "
            "profile/environment check is not armed</span>"
        )
    return f"<span class='muted'>{_esc(profile)}</span> {body}"


def _credentials_cell(trading: str | None, market_data: str | None) -> str:
    """Which tastytrade credential SLOT each session uses. Names, never values.

    Plain text with no colour state, unlike the two rows above it. Nothing here is
    actionable-when-wrong, because wrong cannot get this far: `trading_credentials`
    refuses a live profile whose live slot is absent, and refuses one whose live
    slot carries the cert values. The row is here so the RIGHT state can be
    confirmed on the cutover — the one moment when "which registration is this
    process using?" is the whole question, and the answer used to be unobservable.

    MUTED when absent, matching `_config_cell`: a `Config.from_dict` process (a
    tool, a test) lifts no slots, and that is not a deployed state.
    """
    if trading is None and market_data is None:
        return (
            "<span class='muted'>—</span> <span class='muted'>&mdash; not lifted from "
            "a config; not a deployed state</span>"
        )
    return (
        f"trading <code>{_esc(trading or '—')}</code> "
        f"<span class='muted'>&middot;</span> "
        f"market data <code>{_esc(market_data or '—')}</code>"
    )


def _account_cell(account_number: str | None) -> str:
    """WHICH tastytrade account this process trades.

    No colour state, for the same reason `_credentials_cell` has none and by the
    rule `_config_cell` states outright — colour is ACTIONABLE, not unusual. There
    is no bad state reachable here: live cannot start unpinned (`config.py` refuses),
    and cert is unpinned by DECISION, permanently. An amber "not pinned" would be
    lit on every cert page for the life of the profile, which is a badge that means
    nothing on the day it finally matters.

    So MUTED for the unpinned case, and it SAYS WHY rather than showing a dash: an
    operator reading this on the droplet needs to know the state is deliberate and
    that live refuses it, not wonder whether something failed to load.

    The number rendered is the CONFIGURED one, not one read back from the broker —
    and under a pin those cannot differ, because `session.py`'s filter admits only
    an exact match and answers anything else with a PERMANENT `SessionAborted`.
    Unpinned, the account actually taken is in the session's own startup line
    (`account: <n>`), which is the honest place for it: the process does not know
    which one it got until it logs in, and this row is built before that.
    """
    if account_number is None:
        return (
            "<span class='muted'>—</span> <span class='muted'>&mdash; not pinned; "
            "this process takes the login's first account (accounts[0]). "
            "environment='live' refuses to start unpinned</span>"
        )
    return f"<code>{_esc(account_number)}</code> <span class='muted'>&mdash; pinned</span>"


def _age_text(age: float | None) -> str:
    if age is None:
        return "—"
    if not math.isfinite(age):
        return "never"
    return f"{age:.1f}s"


def _fresh_cell(age: float | None, max_age: float, stale: bool | None) -> str:
    text = f"{_age_text(age)} / {max_age:g}s"
    if age is None:
        return f"<span class='muted'>{_esc(text)} (no active session)</span>"
    cls = "bad" if stale else "ok"
    tag = " STALE" if stale else ""
    return f"<span class='{cls}'>{_esc(text + tag)}</span>"


def _link_cell(connected: bool | None, reconnects: int | None, refresh_failures: int | None) -> str:
    """One streaming link's health.

    Colour follows what is ACTIONABLE, which is not the same as what is nonzero:

      - `connected` False is the fault — red.
      - `reconnects` is muted at any value. A link that dropped and came back is
        the reconnect logic doing its job; colouring it red would recreate exactly
        the false-positive this row exists to remove.
      - `refresh_consecutive_failures` is red only when > 0, i.e. the LAST attempt
        failed and the age is currently growing. It resets to 0 on success, so a
        blip that healed shows nothing — again, deliberately.
    """
    if connected is None:
        return "<span class='muted'>— (no active session)</span>"
    cls, txt = ("ok", "connected") if connected else ("bad", "DISCONNECTED")
    parts = [f"<span class='{cls}'>{txt}</span>"]
    if reconnects:
        parts.append(f"<span class='muted'>reconnects {reconnects} (recovered)</span>")
    if refresh_failures:
        parts.append(f"<span class='bad'>refresh failing ×{refresh_failures}</span>")
    elif refresh_failures == 0:
        parts.append("<span class='muted'>refresh ok</span>")
    return ", ".join(parts)


def _would_submit_cell(snap: StatusSnapshot) -> str:
    """Today's raw `shadow.would_submit` count — and a warning when it is moot.

    The category is SHADOW's terminal action. Outside shadow the entry path takes
    over (`entry.dry_run` / `entry.finished`) and this counter is never written
    again, so a bare `0` here reads as "found nothing" when it means "not the
    counter for this environment". Say so rather than leave the trap.
    """
    body = (
        f"<strong>{snap.would_submit_today_raw}</strong> "
        "<span class='muted'>(raw records, NOT deduped trade ideas)</span>"
    )
    if snap.environment != "shadow":
        body += (
            " <span class='warn'>&mdash; shadow-only counter; in "
            f"{_esc(snap.environment)} see \u201cThis session\u201d below</span>"
        )
    return body


def _session_counts_cell(snap: StatusSnapshot) -> str:
    """Categories that moved this session.

    Sorted BY NAME, not by count, and matching the JSON's ordering. Two reasons,
    both about reading it on a phone: a name-sorted list keeps each category in a
    stable position across reloads, so you learn where to look instead of
    re-scanning; and count-sorting puts the noisiest category first, which buries
    the one that matters — `entry.already_attempted` runs ~250/session against
    ~7 `entry.dry_run`, so "did anything execute?" would sort to the bottom.
    Name order also groups the `entry.*` and `order.*` families together.
    """
    if not snap.session_counts:
        return "<span class='muted'>no active session</span>"
    ordered = sorted(snap.session_counts.items())
    return " ".join(
        f"<span class='muted'>{_esc(cat)}</span>&nbsp;<strong>+{n}</strong>"
        for cat, n in ordered
    )


def _queue_depth_cell(snap: StatusSnapshot) -> str:
    """Unconsumed events sitting in each streamer's UNBOUNDED buffer.

    Only NON-ZERO depths are shown, and only they are flagged. In a healthy
    process every one of these is 0 or 1 — the consumers keep up — so a
    persistently non-zero depth is a producer outrunning a consumer, and a
    CLIMBING one is the accumulation behind the intra-session lag ramp. Showing
    nine zeros every time would bury exactly the number that matters.
    """
    parts = []
    for label, depths in (
        ("md", snap.market_data_queue_depths),
        ("acct", snap.account_queue_depths),
    ):
        for name, depth in sorted(depths.items()):
            if depth:
                parts.append(f"<span class='bad'>{_esc(label)}.{_esc(name)} {depth:,}</span>")
    if not parts:
        if not snap.market_data_queue_depths and not snap.account_queue_depths:
            return "<span class='muted'>— (no active session)</span>"
        return "<span class='ok'>all drained</span>"
    return ", ".join(parts)


def _mb(n: int | float | None) -> str:
    return "—" if n is None else f"{n / (1024 * 1024):.1f} MB"


def _memory_cell(snap: StatusSnapshot) -> str:
    """RSS now, and how much it has grown since this process started.

    Deliberately UNCOLOURED. Nobody has established what normal growth looks like
    here yet, so a red badge would be an assertion the data does not support —
    exactly the false-positive pattern the link-health row was written to avoid.
    """
    if snap.rss_bytes is None:
        return "<span class='muted'>— (/proc unavailable)</span>"
    text = _mb(snap.rss_bytes)
    growth = snap.rss_growth_bytes
    if growth is not None:
        rate = snap.rss_growth_per_hour
        suffix = f", {_mb(rate)}/h {_rate_window_label(snap)}" if rate is not None else ""
        text += f" <span class='muted'>({_mb(growth)} since start{suffix})</span>"
        return text
    return _esc(text)


def _gc_cell(snap: StatusSnapshot) -> str:
    """Cumulative collections per generation, plus the gen-2 rate.

    Gen-2 is the one that matters for the lag ramp: it is the generation whose
    pause scales with the number of live objects, so a rising gen-2 rate next to a
    rising loop lag is the signature that identifies the mechanism.
    """
    g0, g1, g2 = snap.gc_collections
    text = f"gen0 {g0:,} / gen1 {g1:,} / <strong>gen2 {g2:,}</strong>"
    rate = snap.gc_gen2_per_hour
    if rate is not None:
        text += (
            f" <span class='muted'>({rate:.2f} gen2/h {_rate_window_label(snap)})</span>"
        )
    c0, c1, c2 = snap.gc_counts
    text += f" <span class='muted'>· pending {c0}/{c1}/{c2}</span>"
    return text


def _rate_window_label(snap: StatusSnapshot) -> str:
    """"over last 61 min" — the rate is a trailing slope, and the page says so, so
    nobody reads it as a since-start average (which it was until 2026-09-18)."""
    if snap.rate_window_s is None:
        return ""
    return f"over last {snap.rate_window_s / 60.0:.0f} min"


def _loss_budget_json(b: LossBudget | None) -> dict[str, Any] | None:
    if b is None:
        return None
    return {
        "remaining": _num_or_none(b.remaining),
        "daily_max_loss": _num_or_none(b.daily_max_loss),
        "binding": b.binding,
        "unknown_reason": b.unknown_reason,
        "worst_case": _num_or_none(b.worst_case),
        "worst_case_ledger": _num_or_none(b.worst_case_ledger),
        "worst_case_broker": _num_or_none(b.worst_case_broker),
        "cash_today": _num_or_none(b.cash_today),
        "open_worst_case": _num_or_none(b.open_worst_case),
        "working_worst_case": _num_or_none(b.working_worst_case),
        "ledger_complete": b.ledger_complete,
    }


def _loss_budget_cell(b: LossBudget | None) -> str:
    """The loss-budget row. Muted with no session; red when no entry can be sized.

    Red at ZERO-OR-LESS (spent, or unreadable) — the state in which Argus will enter
    nothing — and green otherwise, with the numbers: what is left of the cap, which
    estimate binds and the worst case it is measured from. Not amber on a partly
    spent day: a budget that still holds a lot is the rule working, not a warning.
    """
    if b is None:
        return "<span class='muted'>— (no session)</span>"
    if not b.known:
        return (
            _badge("bad", "UNREADABLE — no entries")
            + f" <span class='bad'>{_esc(b.unknown_reason or 'unknown')}</span>"
        )
    cls = "bad" if b.remaining <= 0 else "ok"
    estimates = f"ledger {_fmt_money(b.worst_case_ledger)}"
    estimates += (
        f", broker {_fmt_money(b.worst_case_broker)}"
        if b.worst_case_broker is not None else ", broker n/a"
    )
    incomplete = (
        " <span class='warn'>ledger incomplete (an unpriced fill)</span>"
        if not b.ledger_complete else ""
    )
    return (
        f"<span class='{cls}'>{_esc(_fmt_money(b.remaining))}</span>"
        f" of {_esc(_fmt_money(b.daily_max_loss))} left"
        f" <span class='muted'>(binds: {_esc(b.binding)}; worst case "
        f"{_esc(estimates)})</span>{incomplete}"
    )


def _pnl_cell(pnl: float | None) -> str:
    if pnl is None or not math.isfinite(pnl):
        return "—"
    cls = "bad" if pnl < 0 else "ok"
    return f"<span class='{cls}'>{_esc(_fmt_money(pnl))}</span>"


def _queue_cell(depth: int, alert_depth: int, maxsize: int, dropped: int) -> str:
    cls = "bad" if depth >= alert_depth else "ok"
    extra = f", <span class='bad'>dropped {dropped}</span>" if dropped else ""
    return (
        f"<span class='{cls}'>{depth}</span> / {maxsize} "
        f"<span class='muted'>(alert at {alert_depth})</span>{extra}"
    )


def _kill_detail(s: StatusSnapshot) -> str:
    if not s.killswitch_engaged:
        return ""
    bits = []
    if s.killswitch_source:
        bits.append(f"source {_esc(s.killswitch_source)}")
    if s.killswitch_since:
        bits.append(f"at {_esc(s.killswitch_since)}")
    if s.killswitch_reason:
        bits.append(f"— {_esc(s.killswitch_reason)}")
    if s.killswitch_restored:
        # Says the two things the timestamp alone does not: the halt predates this
        # process, and a restart is not what clears it.
        bits.append(
            "— inherited by this process; POST /resume clears it, a restart does not"
        )
    return f" <span class='muted'>{' '.join(bits)}</span>" if bits else ""


def _audit_health(s: StatusSnapshot) -> str:
    if s.audit_failed:
        return _badge("bad", "FAILED") + f" <span class='muted'>{_esc(s.audit_failed)}</span>"
    return _badge("ok", "healthy")


def _recent_table(recent: tuple[dict[str, Any], ...]) -> str:
    if not recent:
        return "<p class='muted'>no audit records yet</p>"
    body = []
    for r in recent:
        payload = _esc(r["payload"])
        if len(payload) > 240:
            payload = payload[:240] + "…"
        body.append(
            "<tr>"
            f"<td>{_esc(r['seq'])}</td>"
            f"<td class='nowrap'>{_esc(r['ts'])}</td>"
            f"<td>T{_esc(r['tier'])}</td>"
            f"<td class='nowrap'>{_esc(r['category'])}</td>"
            f"<td class='payload'>{payload}</td>"
            "</tr>"
        )
    return (
        "<table class='recent'><thead><tr>"
        "<th>seq</th><th>ts (UTC)</th><th>tier</th><th>category</th><th>payload</th>"
        "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


# A complete, self-contained document. Theme-aware (a night check on a phone),
# but deliberately minimal — legible, not pretty (spec §5).
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>argus status — {env} — {phase}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0; padding: 1rem; line-height: 1.4; font-size: 16px;
  background: #fff; color: #111; }}
h1 {{ font-size: 1.25rem; margin: 0 0 .25rem; }}
.generated {{ color: #666; font-size: .85rem; margin: 0 0 1rem; }}
table.vitals {{ border-collapse: collapse; width: 100%; max-width: 720px; }}
table.vitals th {{ text-align: left; font-weight: 600; color: #555;
  vertical-align: top; padding: .3rem .6rem .3rem 0; white-space: nowrap; width: 1%; }}
table.vitals td {{ padding: .3rem 0; vertical-align: top; }}
tr.sep td {{ border-top: 1px solid #ddd; padding: 0; height: .5rem; }}
.badge {{ display: inline-block; padding: .05rem .45rem; border-radius: .35rem;
  font-weight: 700; font-size: .8rem; letter-spacing: .02em; }}
.ok {{ color: #0a7d28; }} .bad {{ color: #c0261b; }} .warn {{ color: #a15c00; }}
.muted {{ color: #777; }}
.badge.ok {{ background: #e4f6e8; }} .badge.bad {{ background: #fbe4e2; }}
.badge.warn {{ background: #fbefda; }} .badge.muted {{ background: #ececec; color: #555; }}
h2 {{ font-size: 1rem; margin: 1.5rem 0 .5rem; }}
.scroll {{ overflow-x: auto; }}
table.recent {{ border-collapse: collapse; font-size: .8rem; min-width: 100%; }}
table.recent th, table.recent td {{ border-bottom: 1px solid #eee; padding: .25rem .5rem;
  text-align: left; vertical-align: top; }}
table.recent th {{ color: #555; }}
.nowrap {{ white-space: nowrap; }}
td.payload {{ font-family: ui-monospace, Menlo, monospace; word-break: break-word; }}
footer {{ color: #777; font-size: .8rem; margin-top: 1.5rem; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #16181c; color: #e6e6e6; }}
  h1 {{ color: #f0f0f0; }} .generated, footer {{ color: #9aa0a6; }}
  table.vitals th {{ color: #aab; }} tr.sep td {{ border-top-color: #333; }}
  .ok {{ color: #4ec66f; }} .bad {{ color: #ff6a5e; }} .warn {{ color: #e0a13a; }}
  .muted {{ color: #9aa0a6; }}
  .badge.ok {{ background: #14351d; }} .badge.bad {{ background: #3a1613; }}
  .badge.warn {{ background: #3a2c10; }} .badge.muted {{ background: #2a2d33; color: #aab; }}
  table.recent th, table.recent td {{ border-bottom-color: #2a2d33; }}
}}
</style>
</head>
<body>
<h1>argus — {env} — {phase}</h1>
<p class="generated">generated {generated} · read-only · reload to refresh</p>
<table class="vitals">
{rows}
</table>
<h2>Recent activity</h2>
<div class="scroll">
{recent}
</div>
<footer>Read-only monitoring surface (spec §3/§5). The only state-changing route is
POST /kill.</footer>
</body>
</html>
"""
