"""Runtime configuration for Argus.

Everything that the spec forbids hardcoding lives here and is loaded from a TOML
file (+ a small set of env overrides). In particular:

- **COA auction timing** — Cboe has changed the SPX Complex Order Auction
  response window repeatedly (100ms -> 250ms -> 500ms, plus a separate
  extended-processing allowance). It is a Cboe-configurable parameter and MUST
  come from config, never a literal in the execution loop (spec §7, CLAUDE.md
  non-negotiables).
- **Risk caps** — daily max-loss circuit breaker threshold, concurrent-position
  cap, and starting position size. Conservative until a live track record backs
  the edge up (spec §8).
- **Staleness thresholds** — the fail-closed gate's tolerances (spec §6).

Loading is fail-closed: any missing or nonsensical value raises `ConfigError`
rather than silently falling back to a default that could size a live trade.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

VALID_ENVIRONMENTS = ("shadow", "sandbox", "live")
VALID_SIDES = ("put", "call")
# `signal.short_delta_source`: which delta chooses the short strike. See
# `SignalConfig.short_delta_source`.
SHORT_DELTA_MID = "mid"
SHORT_DELTA_FEED = "feed"
SHORT_DELTA_SOURCES = (SHORT_DELTA_MID, SHORT_DELTA_FEED)
VALID_MARKETDATA_SOURCES = ("sandbox", "live")
# Wildcard bind addresses that expose a socket on EVERY interface, including the
# public one. The kill-switch control surface must never bind these — see
# ServerConfig. Empty string is included because it is asyncio's own "all
# interfaces" sentinel.
WILDCARD_BIND_HOSTS = ("", "0.0.0.0", "::")
MAX_PORT = 65535
# SQLite `PRAGMA synchronous` settings, and the subset that actually survives a
# host-level crash with the last commits intact.
VALID_SYNCHRONOUS = ("OFF", "NORMAL", "FULL", "EXTRA")
DURABLE_SYNCHRONOUS = ("FULL", "EXTRA")
#: Ceiling on `staleness.account_stream_max_age_s` under `environment = "live"`.
#:
#: The value is widened in cert for ONE reason that does not exist in live: the
#: tastytrade sandbox's `/positions` times out often enough that the REST liveness
#: re-seed cannot stamp the account stream (245 failures / 48 outage episodes in one
#: session, 2026-08-27), which denied 8 of 16 candidates on `account_stream_stale`
#: alone. That is a cert artifact. Live has no such excuse, and this rail is what
#: stops Argus trading on an account picture it has no proof is current.
#:
#: Structural rather than a comment, and for the reason the `audit.synchronous`
#: refusal below is: a cert-only relaxation of a fail-closed rail is exactly the
#: kind of thing that rides into live by being forgotten. `greeks_max_age_s` gets
#: NO equivalent ceiling — market data already reads from the live feed, so its
#: burst cadence is production behaviour and its bound is not environment-specific.
LIVE_MAX_ACCOUNT_STREAM_AGE_S = 30.0

# `watchdog.flatten_deferral`'s values (2026-09-25). Mirrors
# `execution.flatten_deferral.DeferralMode`, which this module must not import (the
# loader imports nothing from `execution/`); a test pins that the two agree.
FLATTEN_DEFERRAL_MODES = frozenset({"off", "record", "on"})
# The heaviest-tailed value `watchdog.flatten_defer_tail_alpha` may take the other way
# round: the loader refuses anything ABOVE it (a THINNER tail). 3 is the inverse cubic
# law of index returns; the recorded sample (weeks) cannot certify better.
MAX_FLATTEN_DEFER_TAIL_ALPHA = 3.0

#: Config files are named by PROFILE: `argus.<profile>.toml`. The profile names the
#: file; the file's own `environment` key remains the sole authority on trading
#: behaviour, and `_check_profile` refuses when the two disagree.
#:
#: Note this deliberately does NOT match a bare `argus.toml` — two dots are
#: required. That is what let the mechanism ship one release ahead of the rename
#: (v0.3.17, against a still-`argus.toml` droplet), and it stays true afterwards for
#: an ad-hoc `--config /tmp/x.toml`: no profile, no assertion, and `/status` says so
#: in amber rather than reading as ordinary.
CONFIG_FILENAME_RE = re.compile(r"^argus\.(?P<profile>[a-z0-9][a-z0-9_-]*)\.toml$")
#: A strategy's own file, `argus.<strategy>.<profile>.toml` (2026-10-07, "platform
#: by extraction"). One more dotted part than `CONFIG_FILENAME_RE` above, which
#: therefore never claims one: the SPX loader never reads a strategy's file. Each strategy's loader checks the
#: filename against its own `strategy` and `environment` keys.
STRATEGY_CONFIG_FILENAME_RE = re.compile(
    r"^argus\.(?P<strategy>[a-z0-9_]+)\.(?P<profile>[a-z0-9][a-z0-9_-]*)\.toml$"
)
#: Not a trading environment (`VALID_ENVIRONMENTS`), so it carries no
#: profile==environment equality — only the one-way refusal in `_check_profile`.
PROFILE_DEV = "dev"
#: Hex characters of the config fingerprint. 12 is ~48 bits: far past collision
#: relevance for a set of three files, and short enough to read off a phone.
CONFIG_SHA_LEN = 12


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid. Fail closed."""


@dataclass(frozen=True)
class ConfigSource:
    """Which config file is loaded, and a fingerprint of its bytes.

    The version stamp (`argus._version`) answers "which CODE is running?" and the
    deploy reads it back from the journal, because `argus.service` has no build step
    and a silent reattach to the previous image is otherwise invisible.

    **Config is the half that is explicitly excluded from the image** — it is
    bind-mounted, so it does not roll back with the image tag — and until this
    existed nothing anywhere answered the same question about it. After a
    config-only release these four states were indistinguishable: the new config
    was loaded; the restart happened before the checkout; the mount path drifted;
    somebody hand-edited the droplet and the process has not restarted since.

    A HASH rather than a hand-maintained `[meta] release` string, deliberately: it
    cannot go stale, needs no REQUIRED key (so introducing it forces no coupled
    deploy), cannot be forgotten at tag time, and answers the sharper question —
    *are the bytes I deployed the bytes that are loaded?* rather than *did somebody
    remember to bump it?*
    """

    #: `None` when the path does not match `CONFIG_FILENAME_RE` (an ad-hoc
    #: `--config` path). Rendered as such: a missing profile is a fact, not a
    #: default to invent.
    profile: str | None
    #: BASENAME only. The full path is container-local noise and says nothing an
    #: operator can act on.
    filename: str
    #: First `CONFIG_SHA_LEN` hex of sha256 over the file bytes.
    sha: str


def profile_from_filename(name: str) -> str | None:
    """`argus.live.toml` -> `"live"`. Anything else -> `None`."""
    m = CONFIG_FILENAME_RE.match(name)
    return m.group("profile") if m else None


def _check_profile(profile: str | None, environment: str) -> None:
    """Refuse a config whose filename and `environment` key disagree.

    The profile names the file and nothing else. This is what stops the pairing
    being got wrong by editing one and not the other — and it replaces the old
    `ARGUS_ENV`-overrides-`environment` behaviour, which let an env var point the
    SANDBOX config at the live API. That was fenced only by accident:
    `account_stream_max_age_s = 120 > 30` tripped the live ceiling, and that 120 is
    a cert artifact that could legitimately drop at any time.
    """
    if profile is None:
        return
    if profile in VALID_ENVIRONMENTS and profile != environment:
        raise ConfigError(
            f"config file names profile {profile!r} but declares "
            f"environment = {environment!r}. The filename and the key must agree — "
            f"rename the file or fix the key; do not pick one and hope."
        )
    # `dev` is not a trading environment, so it takes no equality check — but it is
    # the file most likely to be run by hand from a laptop, and the one place a
    # stray `environment = "live"` would route real orders from a dev tree.
    if profile == PROFILE_DEV and environment == "live":
        raise ConfigError(
            "argus.dev.toml must never declare environment = 'live'"
        )


def resolve_config_path(
    env: Mapping[str, str] | None = None,
    *,
    strategy: str | None = None,
) -> tuple[Path, str | None]:
    """Decide which config file to load, from the environment. Returns (path, profile).

    Precedence, and there is deliberately no fourth step:

        1. ARGUS_CONFIG set  -> that path, verbatim.  Explicit beats derived.
        2. ARGUS_ENV set     -> <ARGUS_CONFIG_DIR>/argus.<ARGUS_ENV>.toml
                                (ARGUS_CONFIG_DIR defaults to the working directory)
        3. neither           -> ConfigError. REFUSE.

    `strategy` (a strategy other than the SPX book, chosen by `python -m argus`'s
    registry) changes only the derived name, to `argus.<strategy>.<ARGUS_ENV>.toml`,
    and makes an explicit path name the same strategy. `None` is the SPX book's
    call, unchanged.

    **Step 3 is the point.** The old default was `argus.toml`, resolved against the
    working directory — which from the repo root is the LIVE database, and is the
    mechanism of the 2026-07-21 incident (a bare `python -m argus` took seq
    4128-4130 and wedged a running shadow session's audit writer for ~80 minutes).
    Two enforcement layers were added afterwards and the rule still stood on
    discipline. Removing the default retires it mechanically.
    """
    env = os.environ if env is None else env
    explicit = env.get("ARGUS_CONFIG")
    profile_var = env.get("ARGUS_ENV")
    if explicit:
        path = Path(explicit)
        if strategy is None:
            profile = profile_from_filename(path.name)
        else:
            m = STRATEGY_CONFIG_FILENAME_RE.match(path.name)
            if m is None or m.group("strategy") != strategy:
                raise ConfigError(
                    f"ARGUS_CONFIG={explicit!r} is not a {strategy!r} config: "
                    f"it must be named argus.{strategy}.<profile>.toml"
                )
            profile = m.group("profile")
        # Both set and disagreeing is a real misconfiguration — a systemd unit and a
        # compose file that were edited at different times — and it is silent in
        # every other respect, because precedence would just pick one.
        if profile_var and profile is not None and profile_var != profile:
            raise ConfigError(
                f"ARGUS_ENV={profile_var!r} disagrees with ARGUS_CONFIG={explicit!r} "
                f"(profile {profile!r}). Set one, or make them agree."
            )
        return path, profile
    if profile_var:
        if not CONFIG_FILENAME_RE.match(f"argus.{profile_var}.toml"):
            raise ConfigError(
                f"ARGUS_ENV={profile_var!r} is not a usable profile name "
                f"(lowercase letters, digits, '_' and '-'; must not start with '-')"
            )
        directory = Path(env.get("ARGUS_CONFIG_DIR", "."))
        if strategy is not None:
            name = f"argus.{strategy}.{profile_var}.toml"
            if not STRATEGY_CONFIG_FILENAME_RE.match(name):
                raise ConfigError(f"strategy {strategy!r} is not a usable strategy name")
            return directory / name, profile_var
        return directory / f"argus.{profile_var}.toml", profile_var
    raise ConfigError(
        "no config selected: set ARGUS_ENV (the profile, e.g. 'live' -> "
        "argus.live.toml) or ARGUS_CONFIG (an explicit path). There is no default "
        "— a default path is how the wrong database gets opened."
    )


@dataclass(frozen=True)
class COAConfig:
    """Cboe Complex Order Auction timing. NEVER hardcode these in logic.

    `source` is a required provenance note describing where the value came from
    and when it should be re-checked against Cboe's published product config —
    it exists so a stale window is auditable rather than invisible.
    """

    response_window_ms: int
    extended_processing_ms: int
    source: str


@dataclass(frozen=True)
class RiskConfig:
    """Risk caps. THREE independent size rails, and the tightest one governs.

    `position_size_pct` alone was the only size rail until chunk 5, and it is a
    RELATIVE bound: 2% of NLV is $1,250 only at an NLV of exactly $62,500, so the
    operator's decided per-trade cap was a coincidence of account size rather than
    a stated limit. `max_loss_per_position` and `max_contracts_per_position` are
    the absolute rails, and they are deliberately not derived from each other —
    a dollar cap and a contract cap disagree about which trade is too big as soon
    as credit or strike distance moves, and whichever is more restrictive for the
    trade actually in hand is the one that should bind.

    All three are enforced in BOTH places, on purpose: `orchestrator.size_contracts`
    applies them when choosing a size, and `risk.gate.PreTradeGate` re-checks the
    resulting proposal. The sizer can be wrong (or bypassed); the gate is the rail.
    """

    daily_max_loss: float          # USD; circuit breaker halts new entries at this loss
    #: How many SPREADS may be open at once. All three caps here are per-spread —
    #: that consistency is the fix for 2026-08-13, when this one alone was compared
    #: against a LEG count and `2` therefore permitted exactly one vertical. The
    #: gate reads `RiskSnapshot.open_spread_equivalent`, never the raw leg count.
    max_concurrent_positions: int
    position_size_pct: float       # fraction of account NLV, e.g. 0.02 == 2%
    #: USD ceiling on ONE position's defined risk, independent of NLV.
    max_loss_per_position: float
    #: Hard contract ceiling on ONE position. Distinct from
    #: `max_concurrent_positions`, which counts positions, not contracts.
    max_contracts_per_position: int
    #: THE SIDE-BALANCE RULE (2026-09-26). When true, a new spread may open only on a
    #: side that leaves `|puts − calls| <= max_side_imbalance`, and among the sides
    #: that qualify on a tick the one leaving the book MORE balanced goes first (ties
    #: in `signal.sides` order). Chosen in `orchestrator` and re-checked by
    #: `PreTradeGate` (`side_imbalance_exceeded`), like every size rail. **False is
    #: the pre-2026-09-26 behaviour exactly** — the strategy's first qualifying side,
    #: no side logic — and is the config-only rollback. REQUIRED, strict bool.
    #: → docs/designs/2026-09-26-multi-spread-side-balance.md
    side_balance: bool
    #: The largest `|puts − calls|` an entry may leave. 1 at two slots is "at most one
    #: open spread per side". Inert while `side_balance` is false.
    max_side_imbalance: int

    @property
    def open_risk_is_welded(self) -> bool:
        """`max_concurrent_positions × max_loss_per_position ≤ daily_max_loss`.

        The WELDED size invariant (CLAUDE.md's standing landmine): everything the caps
        can authorize open at once fits inside the day's loss budget. Checked for every
        profile by `tests/test_config_profiles.py`; REQUIRED by the loader for
        `watchdog.flatten_deferral = "on"` (2026-09-25), and re-read by the watchdog —
        one predicate, so the two can never disagree about what it means.
        """
        return (
            self.max_concurrent_positions * self.max_loss_per_position
            <= self.daily_max_loss
        )


@dataclass(frozen=True)
class StalenessConfig:
    market_data_max_age_s: float   # DXLink quote age past which entries are blocked
    #: Greeks age past which a leg may no longer be selected as a SHORT strike.
    #:
    #: SEPARATE from `market_data_max_age_s`, and the split is the point. That bound
    #: is a PRICE bound — argus.toml states its reasoning as "quotes drive pricing" —
    #: and it is right for a quote. Delta is not a price. It is read in exactly one
    #: place, the `min()` in `strategy._select` that picks the short strike, and it
    #: contributes nothing to the credit, the width, the max loss, the sizing, the
    #: gate, or the walk's limit. All of those come off quotes that stay bounded by
    #: `market_data_max_age_s` and stay fresh.
    #:
    #: 2026-08-24 split the ROLE (a fresh quote admits; fresh greeks decide whether
    #: the admitted leg may be short). This is the other half: the two roles no
    #: longer share a THRESHOLD. Measured 2026-08-27, the greeks feed refreshes the
    #: chain in synchronized bursts with gaps of median 137s / p90 421s, so at 2.0s
    #: a short strike was selectable on 2.8% of in-window ticks and
    #: `no_short_leg_with_fresh_delta` was 97% of them.
    #:
    #: What a stale delta actually costs is ONE number: at gamma ~0.0075 delta/pt on
    #: a 0DTE 15-delta SPX strike, one 5-point strike is 0.0374 of delta, and a
    #: 120s-old delta moves the selection by 0.46 of a strike at 1 sigma. It cannot
    #: mis-PRICE anything — only pick a neighbouring strike.
    #:
    #: **NOT a cert-only relaxation, unlike `account_stream_max_age_s`.**
    #: `[marketdata] source = "live"` already, so that burst cadence is the
    #: production feed's own behaviour and this number is the right one in live too.
    #: Deliberately NOT sized to cover the p90 gap: that needs ~420s, which costs
    #: 0.86 of a strike at 1 sigma and 2.6 at 3 sigma. The tail is what a
    #: drift-based bound (gamma x |spot moved since capture|) is for; buying it with
    #: a blunt timer is the wrong instrument.
    #: → docs/decisions/2026-08-24-entry-leg-admission-is-role-scoped.md
    greeks_max_age_s: float
    account_stream_max_age_s: float  # account state age past which entries are blocked
    # Liveness floor. The AlertStreamer only pushes on CHANGE, so a quiet (e.g.
    # flat) account emits nothing and "time since last event" grows without
    # bound — which the staleness gate would misread as a dead link and use to
    # block every entry. A periodic REST re-seed proves the link is alive and
    # refreshes the timestamp. MUST be < account_stream_max_age_s.
    account_refresh_s: float
    # Per-attempt bound on the REST re-seed. Without it the call hangs on the
    # HTTP client's own default, and one slow request eats the whole staleness
    # budget while the loop waits. MUST be < account_refresh_s so a timed-out
    # attempt still leaves room to retry before the next scheduled one.
    #
    # THE per-attempt bound for a `fetch_positions()` round trip, wherever one is
    # awaited — two call sites since 2026-08-21, not one. The other is the
    # watchdog's REST confirm on the paging edge of a book disagreement
    # (`_cache_is_over_reporting`), which awaits inside a `check_once` pass and so
    # blocks trigger evaluation for exactly this long in the worst case. It had
    # been bounded by `watchdog.refresh_interval_s` — a rebuild cadence, 3x the
    # `sustained_s` debounce. Anyone raising this value is raising the watchdog's
    # worst-case blind window by the same amount.
    #
    # Since 2026-08-26 the refresher spends it as a TOTAL budget across a
    # SERIALIZED pair (positions, then balances) against one monotonic deadline,
    # rather than as one wrapper around a concurrent `gather`. That is not a
    # semantic change: it was already the bound on one whole attempt, and for the
    # watchdog's single request "total budget" and "per-request bound" are the
    # same number. Making it per-REQUEST was rejected — 2 x 5s is a 10s worst case,
    # equal to account_refresh_s, which breaks the invariant enforced below.
    # → docs/decisions/2026-08-26-account-refresh-off-the-event-loop.md
    account_refresh_timeout_s: float
    # How long a cached position absent from every REST snapshot must go untouched
    # before the re-seed drops it (`AccountStateCache.evict_absent`). Exists because
    # a closed position is OMITTED from the REST snapshot rather than reported flat,
    # so absence is the only evidence there is — and absence alone is not enough:
    # the broker's book trails a fill by ~4-5s, and evicting inside that window
    # would drop a real position and hand out a concurrent slot already spoken for.
    #
    # ITS OWN KEY, never a reuse of watchdog.book_settle_s. Both are sized against
    # the same propagation lag, which is exactly why sharing one number would weld
    # two unrelated conditions together — the same reasoning tools/evaluability.py
    # records for the blind-episode allowance. MUST be > account_refresh_s: a real
    # position is re-touched by every seed, so it can never age past the interval,
    # and a threshold at or below it would evict on timing jitter alone.
    account_position_evict_after_s: float


@dataclass(frozen=True)
class AuditConfig:
    db_path: str
    tier2_queue_maxsize: int       # bound on the telemetry queue (spec §6 — never unbounded)
    tier2_queue_alert_depth: int   # depth at which to alert before the queue fills
    # SQLite durability posture. NOT a literal in connection.py, because the
    # right answer differs by environment: shadow submits nothing and can afford
    # NORMAL; live cannot, since the log's tail is what reconciliation reads back
    # as expected state. Required (no default) — a silent default here is exactly
    # the kind of unexamined choice that goes unnoticed until a crash.
    synchronous: str


@dataclass(frozen=True)
class HeartbeatConfig:
    """Event-loop-lag heartbeat (spec §4, §9).

    A watchdog for the watchdog: measures how late its own periodic wakeups fire,
    which is a direct proxy for the event loop being blocked. A stalled loop can't
    react to a gamma spike or a kill-switch press, so this is the early-warning
    signal for the one failure the single-loop design can least afford.

    `trip_on_sustained_stall` is the §11 open question (Richard's call): whether a
    sustained stall should trip the same fail-closed halt as stale data. Defaults
    to False (alert-only) here so the decision is an explicit config flip, not a
    silent default.
    """

    interval_s: float               # how often the heartbeat wakes
    lag_alert_threshold_s: float     # wake-lateness past which a breach is flagged
    sustained_breach_count: int      # consecutive breaches that count as "sustained"
    trip_on_sustained_stall: bool    # engage the kill switch on a sustained stall


@dataclass(frozen=True)
class SignalConfig:
    """Baseline credit-spread signal parameters (spec §8, §11).

    These are shadow-mode tuning knobs, NOT a validated edge — they exist so the
    strategy's thresholds live in config and get calibrated from shadow data
    rather than hardcoded. `sides` is which verticals to consider (put credit /
    call credit). `entry_earliest`/`entry_latest` are ET wall-clock bounds.
    """

    sides: tuple[str, ...]
    target_short_delta: float    # delta magnitude for the short strike, e.g. 0.15
    width: float                 # DESIRED spread width in points
    # Hard cap on the actual strike distance. Real ladders have gaps, so the long
    # leg is the nearest strike at least `width` further OTM; `max_width` bounds
    # how much extra risk a coarse ladder may introduce (economics always use the
    # ACTUAL strike distance, never the desired one).
    max_width: float
    min_credit: float            # minimum net credit (points) to accept
    entry_earliest: time         # ET
    entry_latest: time           # ET
    cadence_s: float             # how often the loop evaluates
    #: WHICH delta chooses the short strike (2026-09-21). "mid": the delta implied
    #: by each leg's own mid, the same quotes the credit is computed from, so
    #: `min_credit` cannot select on the feed delta's estimation error. "feed": the
    #: greeks feed's delta, the pre-2026-09-21 behaviour and the config-only
    #: ROLLBACK (after the close, never mid-session with a position open). REQUIRED,
    #: no default, enum-checked — it changes which strike is traded. The mode that
    #: does not select still records its pick as `alt_pick_strike`.
    #: → docs/decisions/2026-09-21-short-strike-selection-on-mid-implied-delta.md
    short_delta_source: str


@dataclass(frozen=True)
class ExecutionConfig:
    """Order construction bounds and price-walking parameters (spec §7).

    `signal_max_age_s` is how stale a candidate may be at the moment its order is
    BUILT — measured from the chain snapshot the candidate came from, not from
    when the loop woke up. It exists so a pipeline stall between the pre-trade
    gate passing and the order being constructed is caught by a timestamp
    comparison rather than by a fresh quote fetch: refetching would submit a
    different price than the one that was sized and gated (and would add a round
    trip on the latency-critical path). Keep it TIGHT — well inside
    `staleness.market_data_max_age_s`, since it bounds a much shorter window: the
    handful of milliseconds between gate-pass and construction, not the age of
    the feed itself.

    **The `entry_*` values are strategy parameters, not engineering constants.**
    They decide how much credit a live entry gives up chasing a fill, which is a
    direct claim on expectancy, and they belong to the operator the same way the
    holiday calendar and the COA window do (spec §7/§11). `walk_source` is a
    required provenance note in the same spirit as `coa.source`: it makes "these
    are still the placeholder numbers" a fact stated in the deployed config and
    visible in every review, rather than a comment that gets lost. Exit-side
    parameters are NOT here — chunk 4 builds its own `WalkPolicy`, which is the
    point of the policy being a parameter.

    `max_order_requests_per_second` is a CEILING, not a target. tastytrade support
    confirmed 40 req/s per IP on `/orders` (2026-07-29); the strategy's real volume
    is under 1/s. See `execution/ratelimit.py` for why a governor exists anyway.
    """

    signal_max_age_s: float
    #: Seconds an entry order is held at each price before repricing (spec §7's
    #: "wait one cycle"; ~3-5s). Raised to the COA settle window if configured
    #: below it — cancelling inside the auction races a straggler response.
    entry_cadence_s: float
    #: Ticks conceded per reprice step on an entry walk. Spec §7 suggests 1 ($0.05).
    entry_step_ticks: int
    #: Fraction of the original limit's magnitude an entry may concede before
    #: abandoning. Spec §7's "e.g. 10% of max profit" is 0.10.
    entry_walk_away_pct: float
    #: POINTS of underlying movement, away from the price the candidate was gated
    #: at, that abandon an entry walk (`WalkOutcome.DRIFTED`). The ladder steps
    #: from a mid fixed at gate time and never re-quotes, which keeps the walk
    #: deterministic and its concession auditable — but at 10-15 delta a 2-3 point
    #: SPX move inside a 20-30s walk can move a 5-wide spread by a large fraction
    #: of its credit, and the remaining rungs are then priced off a market that has
    #: moved on. This is the circuit breaker for that, NOT a re-quote.
    #:
    #: A strategy parameter, like the three above and covered by the same
    #: `walk_source` note: too tight abandons entries on ordinary noise, too loose
    #: is a check that never fires. Points, not a formula — a formula would need
    #: assumptions about delta and width that belong to the strategy, not here.
    entry_drift_abort_points: float
    #: Where these numbers came from and when to revisit them. Same role as
    #: `coa.source`; a blank value is refused.
    walk_source: str
    #: How long to wait for a submitted order to reach a confirmed in-market
    #: status before cancelling it. Bounds the measured pre-market case, where an
    #: accepted order sits at `Received` with `tif.next_valid_session`.
    working_timeout_s: float
    #: How often to poll an order's status while waiting for it to reach the
    #: market. Operational only — trades request volume against reaction time.
    status_poll_interval_s: float
    #: Consecutive broker failures a walk tolerates before returning UNRESOLVED.
    max_recovery_attempts: int
    #: How long the broker gets to answer ONE duplicate-guard check
    #: (`DuplicateOrderGuard.has_conflicting_order` -> `get_live_orders`). Applied
    #: by the CALLER as an `asyncio.timeout`, exactly as `[reconcile]
    #: request_timeout_s` is and for the same reason: the execution port is narrow
    #: by design and a timeout argument on every method is the wrong seam.
    #:
    #: It exists because the alternative is an INHERITED default. Without it the
    #: bound is `broker.http_timeout_s` (30s), which that key's own comment sizes
    #: for "a cold FIRST authenticated call from a new host" — and because httpx
    #: reads a scalar `timeout=` as connect/read/write/pool INDEPENDENTLY (the trap
    #: `account/refresher.py` was fixed for on 2026-08-26), one guard call could
    #: spend 30s connecting and 30s again reading. That is on the entry path, whose
    #: candidate was built against a 1.0s freshness bound.
    guard_timeout_s: float
    #: Consecutive EXHAUSTED guard bursts (not single failures) before today's
    #: ENTRIES end. One blip retires one trade idea; this many in a row is sustained
    #: blindness and takes the original `_end_entries_today` posture. Counts DISTINCT
    #: trade ideas, not ticks — see `entry.py`. Since 2026-09-01 the stand-down is
    #: entries only: the watchdog, the exits and the 15:30 flatten run on.
    guard_unreachable_max_streak: int
    #: Process-wide ceiling on order requests per second. The broker's limit.
    max_order_requests_per_second: float
    #: ACTIVATION MODE for the entry path (chunk 5). True runs the whole pipeline —
    #: gate -> duplicate guard -> `build.build_order` -> the audited/throttled stack
    #: -> the broker — but submits with the broker's own `dry_run=True`, so the
    #: wiring is exercised end-to-end with no order at risk. It stops there rather
    #: than walking: a dry run mints NO order id (measured, chunk 1), so there is
    #: nothing for the price-walking loop to poll, reprice or cancel, and pretending
    #: otherwise would test the loop against a fiction.
    #:
    #: DEFAULTS TO TRUE when the key is absent, which is the one place in this
    #: loader that defaults rather than refusing. The fail-closed direction for an
    #: order-routing switch is "do not route": flipping `environment` to sandbox
    #: against a config that predates this key must not start firing real orders by
    #: omission. Set it to false deliberately, as its own reviewable line.
    dry_run_submissions: bool


@dataclass(frozen=True)
class WatchdogConfig:
    """Position risk watchdog + exit handoff (spec §4, §7 exit variant, §8).

    The watchdog is a SEPARATE task from the signal engine (spec §4: "gamma risk
    in the final hour doesn't care whether entry logic is running"), so its
    parameters live in their own section rather than under `[execution]`, which is
    about order mechanics.

    **The trigger values are strategy parameters, not engineering constants**, in
    exactly the sense `execution.entry_*` are: `credit_multiple` and
    `underlying_buffer_points` decide when a losing spread is closed, which is a
    direct claim on the loss distribution — spec §8 notes that the watchdog's
    virtual stop is what should keep realized losses well under the theoretical
    max-loss column, and these two numbers ARE that stop. `trigger_source` is the
    required provenance note, same rule as `coa.source` and `execution.walk_source`:
    a blank value is refused, so "still the placeholder numbers" is a fact stated
    in the deployed config rather than a comment somebody edits around.

    `sustained_s` is the debounce, not a delay: spec §7's exit variant calls for a
    threshold "sustained for several consecutive seconds to filter out a momentary
    spread blowout". A 0DTE spread's mid can spike through 2x credit on one wide
    print and come straight back; acting on that single observation is how a
    momentary quote gap becomes a realized loss.
    """

    #: How often open positions are re-evaluated against the triggers.
    cadence_s: float
    #: How often the watch list is rebuilt from the order ledger. Deliberately
    #: MUCH slower than `cadence_s`: the ledger read runs on the audit log's
    #: writer-connection lock (`AuditLog.read`), which a crash-critical Tier 1
    #: write also takes, and that lock's contract is "reads are rare". Trigger
    #: evaluation itself is pure in-memory quote arithmetic and needs no database.
    refresh_interval_s: float
    #: How long a `booked_but_absent` book disagreement (broker < ledger) may
    #: persist before it pages. ONE DIRECTION ONLY as a blanket timer:
    #: `held_but_unbooked` is the dangerous one — a real position nobody is
    #: watching — and never waits on a clock alone. Since 2026-08-20 this value
    #: ALSO bounds how long Argus's own closing-fill record stays admissible as
    #: the evidence that suppresses a `held_but_unbooked` disagreement
    #: (`PositionWatchdog._closing_fill_cover`) — same physics, same lag, so the
    #: same knob rather than a second one that would drift from it. That is
    #: evidence-gated, not time-gated: no closing fill, no allowance.
    #: The live-fire pass on 2026-08-13 showed `booked_but_absent` is ROUTINE
    #: post-fill propagation: Argus's ledger books a fill from the AlertStreamer
    #: before the broker's own position book reflects it, sometimes leg by leg
    #: (`broker_legs: 1`), resolving in 4-5s both times it happened. Without an
    #: allowance that is one ERROR page per entry for ordinary behaviour, which
    #: is the 2026-08-05 alert-storm shape: a real signal made audible at the
    #: wrong threshold. TIME-based, not tick-based, because the check runs from
    #: `_refresh_if_due` — `refresh_interval_s` OR whenever a fill calls
    #: `request_refresh()`, so the cadence is irregular by design. 0 disables the
    #: allowance and restores the pre-2026-08-13 instant page.
    #:
    #: **Governs TWO post-fill windows** (2026-08-17), because they are the same
    #: phenomenon and a second key meaning the same thing would only drift from
    #: this one: the book disagreement above, and a spread whose legs have not all
    #: booked yet. The two legs of ONE multi-leg order arrive as separate fill
    #: events (~0.25s apart), and `on_filled` refreshes the watchdog the moment the
    #: first lands — so it aims straight at the window where a spread legitimately
    #: has one applied leg. An `unattributed` position row — a leg no order
    #: explains — still pages on the first observation with no window at all:
    #: there is no pending second leg that would resolve it, so waiting only
    #: delays the page. `held_but_unbooked` is the third window, and the one that
    #: is evidence-gated rather than time-gated (see above).
    book_settle_s: float
    #: How long one REST book-confirm verdict may be reused for the SAME
    #: `(broker_legs, ledger_legs)` shape before it must be re-measured
    #: (`PositionWatchdog._cache_is_over_reporting`, 2026-08-21). Measured from when
    #: the verdict was taken, never extended by a cache hit.
    #:
    #: ITS OWN KEY rather than a reuse of `staleness.account_position_evict_after_s`
    #: — the same rule that key states for itself against `book_settle_s`, and for
    #: the same reason. The eviction window and this one are sized against the same
    #: physics (how long the account cache may be wrong before REST repairs it), and
    #: that is exactly why sharing one number would weld two unrelated conditions
    #: together: one governs when a stale cache entry is DROPPED, this one governs
    #: how long a suppression of the `held_but_unbooked` page may stand on an old
    #: reading. Whoever tunes the first is not thinking about the second.
    #:
    #: What it bounds, in both directions:
    #: - UPWARD, silence. A `True` verdict suppresses the one page that has no
    #:   timer allowance at all. Before this key the verdict was trusted for the
    #:   whole disagreement episode — on 2026-08-19 that was over an hour — so a
    #:   genuinely held, unbooked position arriving at a shape already ruled cache
    #:   drift went unreported for as long as the episode lasted. This is the cap on
    #:   that.
    #: - DOWNWARD, blind time. The confirm is awaited INSIDE a `check_once` pass, so
    #:   each re-measure costs up to `staleness.account_refresh_timeout_s` with no
    #:   position evaluated against its triggers. MUST be > `refresh_interval_s` (the
    #:   loader refuses otherwise) or a steady shape re-confirms on every observation
    #:   and the frequency bound that made the blind window survivable is gone.
    #: → docs/decisions/2026-08-21-book-confirm-cache-is-per-shape-and-expires.md
    book_confirm_ttl_s: float
    #: Trigger when the cost to CLOSE reaches this multiple of the credit received.
    #: 2.0 is the well-established convention for this strategy shape (exit at a
    #: loss equal to the credit collected). Must be > 1: at 1.0 the trigger fires
    #: at break-even, which is an instant exit rather than a stop.
    credit_multiple: float
    #: Trigger when the underlying comes within this many points of the SHORT
    #: strike. The backstop for the case the mid does not price fast enough. 0 is
    #: valid and means "trigger when the strike is touched".
    underlying_buffer_points: float
    #: How long a trigger condition must be CONTINUOUSLY true before acting.
    #: Must be >= `cadence_s`, or a single observation satisfies it and the
    #: debounce does nothing.
    sustained_s: float
    #: Exit-walk cadence. Spec §7: exits run faster than entries (~1s vs 3-5s).
    #: Still floored at the COA settle window by `WalkPolicy.cycle_wait_s`.
    exit_cadence_s: float
    #: Ticks conceded per reprice on the exit walk.
    exit_step_ticks: int
    #: Fraction of the exit mid the walk may concede before abandoning to the
    #: market-crossing order. On a -1.70 debit, 0.20 walks to -2.04.
    exit_walk_away_pct: float
    #: FLOOR under `exit_walk_away_pct`, in REPRICES (2026-08-31). A percentage of a
    #: price on a tick grid vanishes below half a tick: at 0.20 an exit opening at a
    #: debit of 0.05 or 0.10 gets a walk-away floor equal to its own opening limit and
    #: therefore ZERO rungs — which is the ordinary state of a winning 0DTE spread
    #: being flattened at 15:30, not a tail. This is the minimum number of reprices
    #: the walk must have whatever the percentage rounds to.
    #:
    #: STEPS, not ticks, so the guarantee survives `exit_step_ticks` being raised.
    #: `>= 1` — a value of 0 restores the defect, and there is no reason to want a
    #: walk with no walk in it. Raising it is how the owed data-backed pass buys the
    #: near-tick exit more range without a deploy.
    #: → docs/decisions/2026-08-31-exit-ladder-near-tick.md
    exit_walk_min_steps: int
    #: Bounded wait for an ABANDONED exit order to actually go terminal before the
    #: market-crossing order is submitted. `cancel()` leaves an order at
    #: `Cancel Requested`, not gone (see `execution/loop.py`), and stacking a
    #: second closing order on one that can still fill would close the spread
    #: twice — re-opening it, inverted, unmanaged, on expiry day.
    exit_cancel_settle_s: float
    #: How long a TRIGGERED position may go with no exit order in the market before
    #: a second, louder page fires (2026-08-20). Both no-order paths — construction
    #: refused (`watchdog.exit_unattempted`) and legs unpriceable
    #: (`watchdog.exit_unpriceable`) — retry every cadence tick forever, because
    #: giving up would strand an open 0DTE position and burning the one attempt on
    #: an order that was never sent would be worse. The retry is right; the SILENCE
    #: was not. Rising-edge escalation pages once and then goes quiet, so this is
    #: the bound on that quiet: one further Tier 1 + page per episode, no storm.
    #: Must be > 0 — there is no "off", because an exit that can never be built is
    #: never a condition to stop reporting.
    exit_unattempted_max_s: float
    #: How long BEFORE that day's close every held spread is flattened, regardless
    #: of price (2026-08-28, P2). The third trigger, and the only one with no market
    #: input at all.
    #:
    #: **An OFFSET, deliberately, and there is no absolute-time form.** A literal
    #: 15:30 falls two and a half hours after a half-day's 13:00 settlement, so the
    #: control would never fire on the compressed sessions it is most needed —
    #: the same defect the expired-position rule was fixed for on 2026-08-25 ("never
    #: a literal 16:15, which is wrong on a half-day"). The offset is applied to
    #: `MarketCalendar.close_time(d)`, so 1800.0 is 15:30 ET normally and 12:30 on a
    #: half-day, with no branch anywhere.
    #:
    #: **A STRATEGY parameter, in the same class as `credit_multiple`**: it decides
    #: when a losing 0DTE position is closed regardless of what the market says, so
    #: it moves with `trigger_source` like the other three. It exists because the
    #: two price triggers share a dependency the strategy cannot rely on at the one
    #: moment it matters — on 2026-08-28 the credit stop fired at 10:37, the exit was
    #: refused, and the position sat held for 76 minutes because the only thing that
    #: could close it was the thing that had already failed.
    #:
    #: Bounded on both sides and both bounds are real:
    #: - `> 0`. A flatten AT the close is not a flatten — the session has ended, the
    #:   watchdog is stopped, and the settlement print is already made.
    #: - Strictly less than the SHORTEST session the calendar can produce
    #:   (`market.half_day_close - market.session_open`), or the mark falls before a
    #:   half-day's open and every position is born triggered. Checked across
    #:   sections, after `[market]` is parsed.
    #: → docs/decisions/2026-08-28-time-based-flatten-trigger.md
    flatten_before_close_s: float
    #: How long an order Argus has STOPPED MANAGING may remain confirmed-working at
    #: the broker before it pages again — and the floor on how often its cancel is
    #: re-requested (2026-08-29, P5).
    #:
    #: Two quantities in one number because they are the same bound seen from both
    #: sides: past the geometric backoff, the recheck makes one attempt and one page
    #: per interval, so the operator tunes "how often do I hear about this, and how
    #: often does it try again" as one thing.
    #:
    #: **A RATE bound, deliberately not a COUNT** — the opposite of P0's
    #: `_EXIT_SEND_ATTEMPTS`, which is a count because each exit retry costs a
    #: SUBMISSION. A cancel is not a submission, so only the volume argument
    #: survives, and a count would have been wrong on the incident this is for:
    #: cert 502-ed from 10:00 to 10:13 and the order sat working until 12:15, so a
    #: budget of three attempts would have been spent inside the outage and never
    #: retried after recovery.
    #:
    #: MUST be > `refresh_interval_s` (the loader refuses otherwise) — a bound at or
    #: below the poll cadence fires on every tick, which is the alert storm the
    #: `_Escalation` bound exists to prevent.
    #:
    #: A key rather than a constant because it is an ALERT CADENCE, which is the
    #: category an operator actually tunes; the burst's SHAPE (the geometric backoff)
    #: stays in code.
    #: → docs/decisions/2026-08-29-mid-session-cancel-recheck.md
    unmanaged_order_max_s: float
    #: Whether the residual-order recheck actually SENDS the re-cancel (2026-08-29).
    #:
    #: `false` is `would_cancel` mode: the same registry and the same decision —
    #: direction sourcing, the OPEN/CLOSE split, the identifier cross-check, the
    #: working/terminal/absent reading — computed and recorded at Tier 2, with the
    #: outbound call not made. `shadow.would_submit`'s exact shape, and the reason a
    #: staged rollout earns its cost here: every residual it registers exercises the
    #: inputs on real cert data with nothing at the broker's end.
    #:
    #: The SECOND defaulting key in this loader (`execution.dry_run_submissions` is
    #: the other), and it defaults the same way: to the side that sends nothing, so a
    #: config that predates P5 cannot start cancelling orders on an image upgrade.
    unmanaged_order_cancel: bool
    #: Whether the PROFIT TARGET may send (2026-09-23). REQUIRED, no default.
    #:
    #: The fourth trigger (`TriggerReason.PROFIT_TARGET`): a held spread whose net mid
    #: rounds to exactly one tick — the cheapest closing limit that can be expressed —
    #: is bought back then rather than carried to the 15:30 flatten, which pays the
    #: same tick hours later with width-sized gamma in between.
    #:
    #: **The trigger is EVALUATED and RECORDED either way; this decides only whether it
    #: may ACT.** `false` changes no order, no escalation and no page — `watchdog.check`
    #: simply carries `profit_target` in `true_reasons` when it would have fired, which
    #: is exactly the replay input an observe mode would have produced (§11.6 rejected a
    #: tri-state for that reason). Promotion and rollback are a CONFIG FLIP, on
    #: `unmanaged_order_cancel`'s precedent — which matters most for a new live
    #: behaviour whose measured benefit rests on n=2.
    #:
    #: REQUIRED rather than defaulting, unlike `unmanaged_order_cancel`: the decision
    #: record settled one new required key and a COUPLED image + config deploy, so the
    #: value is stated in every profile rather than inherited from the loader.
    #: → docs/decisions/2026-09-22-profit-target-and-re-entry.md §11
    profit_target_enabled: bool
    #: Whether the CREDIT trigger (`credit_multiple`) may fire (2026-09-26). False is
    #: the strike-touch stop: the emergency exit is the underlying trigger alone, and
    #: with `underlying_buffer_points = 0` that is "spot reached the short strike".
    #: The mark is still computed and recorded (the profit target and `blind` share
    #: its branch); only the credit trigger's FIRING is switched. REQUIRED, strict
    #: bool; true is the pre-2026-09-26 behaviour and the config-only rollback.
    #: → docs/decisions/2026-09-26-strike-touch-stop.md
    credit_trigger_enabled: bool
    #: Whether a partial close's STUB may be closed by its ONE further close.
    #: (2026-09-24) REQUIRED, no default.
    #:
    #: A closing order that fills part of the position leaves a stub — fewer
    #: contracts of the same vertical, still held — under the same
    #: `argus-close-*` key the guard blocks on. The continuation is the watchdog
    #: detecting that stub scan-side and handing it ONE further close, on the
    #: ORIGIN's ladder, through the guard's new quantity rule (`closed_filled +
    #: q <= opened_filled`, per leg, from the broker's own fills).
    #:
    #: **It arms BOTH halves, so `false` is exactly the pre-2026-09-24
    #: behaviour**: no handoff, no `watchdog.stub_close` record, AND the
    #: duplicate guard built without its quantity rule
    #: (`pipeline.build_execution_stack` passes this switch as
    #: `close_quantity_rule`) — so the flatten override and every exit retry
    #: after a filled close are BLOCKED on the fill, as they always were. The
    #: guard's rule only ever CLEARS a `conflicting_filled_order` refusal and
    #: fails closed on every unreadable input; the review of 2026-09-24 put it
    #: behind this switch so the rollback is complete rather than half.
    #:
    #: Ships `true` in all three profiles — not `false`-then-promote — because
    #: the emergency-stub exposure exists TODAY with the profit target off, and
    #: discharging the profit target's promotion gate is the point of the build.
    #: `unmanaged_order_cancel`'s precedent for the rollback: a config flip and
    #: a restart.
    #: → docs/designs/2026-09-24-quantity-aware-guard-for-close-identifiers.md §5.6
    stub_close_enabled: bool
    #: Where `credit_multiple` / `underlying_buffer_points` / `sustained_s` /
    #: `flatten_before_close_s` came from and when to revisit them. Refused if blank.
    trigger_source: str
    #: Whether a far-OTM `FLATTEN_BEFORE_CLOSE` may be DEFERRED (2026-09-25):
    #: `"off"` | `"record"` | `"on"`. REQUIRED strict string. `on` defers only when
    #: `execution.flatten_deferral.decide` finds the flatten's insurance at least `k`
    #: times overpriced, for a position nothing has been sent for, whose backstop can
    #: see and whose broker is answering — re-checked every tick. `record` computes
    #: and records the same decision and flattens exactly as `off`.
    #: → docs/designs/2026-09-25-deferring-a-far-otm-flatten.md §4
    flatten_deferral: str
    #: `k`: decline the flatten's insurance only when its premium (one tick) is at
    #: least this many times the expected loss it covers. POLICY — raising it is the
    #: safe direction (fewer deferrals) and needs no analysis; lowering it needs a
    #: `tools.flatten_calibration` rerun and a decision record (§4.9). Refused `< 1`.
    flatten_defer_insurance_multiple: float
    #: `z99`: the calibrated 99th percentile of the volatility- and time-standardized
    #: 30-minute excursion. A CALIBRATION OUTPUT of `tools.flatten_calibration`.
    flatten_defer_tail_z99: float
    #: `alpha`: the calibrated Pareto tail index. Refused outside `(1, 3]` — 3 is the
    #: well-established index-return tail, and a sample of weeks cannot certify a
    #: thinner one.
    flatten_defer_tail_alpha: float
    #: Where `z99` / `alpha` / `k` came from: the tool version, the databases, the
    #: sessions and the date, and the k decision. Refused if blank.
    flatten_defer_source: str


@dataclass(frozen=True)
class ChainConfig:
    """Option-chain subscription parameters.

    TWO DIFFERENT SYMBOLS — conflating them yields an empty 0DTE chain:
    - `chain_symbol` is the option ROOT to fetch the chain from. 0DTE weeklies
      live under "SPXW"; "SPX" returns only monthly third-Friday expirations.
    - `underlying_symbol` is the INDEX to quote for spot ("SPX"). An option root
      cannot be quoted as an index.

    `strike_window_points` bounds how much of the (very large) 0DTE chain we
    subscribe to — centered on spot, since the strategy only works near the money.
    """

    underlying_symbol: str        # index for the spot quote, e.g. "SPX"
    chain_symbol: str             # option root carrying 0DTE, e.g. "SPXW"
    strike_window_points: float   # subscribe strikes within +/- this of spot


@dataclass(frozen=True)
class RegimeConfig:
    """Entry-time market-regime gate — research-motivated, NOT a validated edge.

    Rationale: an unconditional fixed-delta credit spread has a thin,
    execution-cost-fragile edge at 0DTE horizons — realistic backtests put the
    median realized variance risk premium from a 10:00 ET entry at roughly 0.001%
    of underlying, dwarfed by tail risk (1% expected shortfall of ~0.6-1.6% of
    underlying). The strongest documented improvement comes from conditioning
    entries on a small set of transparent state variables observed at decision
    time, not from exotic strike selection. This section holds two of those:

    - `atm_iv_*`: same-day ATM implied vol (annualized decimal), used to bucket
      the entry into a low/mid/high regime. NOTE this is the 0DTE chain's own
      implied vol (VIX-style estimator over the SPXW 0DTE strip, see
      `signal/regime.py`), not the 30-day CBOE VIX index — those diverge,
      especially intraday.
    - `max_*_skew_for_*`: caps on implied skew (up-variance minus down-variance
      across the OTM strip) before a side is refused. Skew beyond a threshold
      means one side of the chain is pricing materially more stress than the
      other; selling into that side is the extrapolation flagged below.

    Caveat to weigh before trusting this in sandbox/live: the academic evidence
    behind conditional timing (VIX-tercile + skew state, logistic-classifier sign
    prediction) was measured on iron butterfly/condor and ratio-spread
    structures, not single-sided credit verticals. The skew-based side gate here
    is a plausible, testable extrapolation to Argus's structure — not something
    directly evidenced in the literature. Validate it against shadow-mode
    `RegimeSnapshot` telemetry before it ever gates a real order, same discipline
    as the base signal params.

    **THIS SECTION IS DELIBERATELY ABSENT FROM `argus.toml`.** `[regime]` missing
    means `Config.regime is None`, which makes both the side gate and the size
    scaler unconditional no-ops (see `signal/strategy.py`). The regime is still
    COMPUTED and LOGGED every tick — that telemetry is what these thresholds have
    to be calibrated from, and it has to run for weeks-to-months before any number
    put here would mean anything. Adding this section is a live behaviour change.

    Every multiplier is a *risk-reduction* knob only (validated <= 1), consistent
    with the fail-closed philosophy: this section can shrink size, never inflate
    it above what `risk.position_size_pct` already allows.
    """

    atm_iv_low_max: float              # ATM 0DTE IV at/below this => "low" regime
    atm_iv_high_min: float             # ATM 0DTE IV at/above this => "high" regime
    max_downside_skew_for_puts: float  # refuse put entries beyond this much downside skew
    max_upside_skew_for_calls: float   # refuse call entries beyond this much upside skew
    size_multiplier_low: float         # position-size multiplier in low-IV regime, <= 1
    size_multiplier_mid: float         # position-size multiplier in mid-IV regime, <= 1
    size_multiplier_high: float        # position-size multiplier in high-IV regime, <= 1


@dataclass(frozen=True)
class EventConfig:
    """Same-day macro-event entry blackout.

    Same non-negotiable as COA timing: the dates that matter (FOMC, CPI, NFP,
    PCE) drift and must come from data, not a literal in the execution loop.
    `blackout_dates_path` points at a small file of ISO dates generated by
    `argus.tools.blackout_dates`. This is a *pattern* reuse of TOMIC's
    economic-calendar approach (FRED release IDs; FOMC dates stay manual by
    design), not a code or credential reuse — Argus keeps its own file, own
    script, own FRED key, per the "Relationship to TOMIC" rule in CLAUDE.md.

    UNLIKE `[regime]`, this ships ENABLED. There is no threshold to calibrate —
    it is a categorical date list, and standing down around FOMC/CPI/NFP/PCE is
    practitioner convention for 0DTE, not a hypothesis under test. Argus's own
    shadow data agrees: 2026-07-29 (FOMC) was the largest-magnitude loss day in
    the would-submit tally to date.

    Phase-1 scope is a whole-day blackout (no new entries at all that date).
    CPI/NFP print before the open and FOMC decisions land at 14:00 ET, so a
    whole-day blackout is conservative but simple; a partial-day version (trade
    before an 08:30 print, stand down only after it) is a reasonable phase-2
    refinement once Argus has the release *times*, not just dates.
    """

    blackout_dates_path: str
    enabled: bool


@dataclass(frozen=True)
class MarketConfig:
    """When the always-on supervisor may run a session — scheduling correctness,
    NOT strategy. (spec §4; the entry *window* inside a session is `[signal]`.)

    The supervisor is always-on but the market is not. A session runs only on a
    trading day, from `session_open` (a few minutes after the 09:30 open, to let
    the chain warm up) to the close. `half_days` close early at `half_day_close`;
    `holidays` are full closures. Both are operator-maintained lists of ISO dates
    — the market calendar is not derivable from code, so it lives in config like
    every other value the spec forbids hardcoding.

    A date in BOTH `holidays` and `half_days` is treated as a full closure
    (holidays win). This is deliberately SEPARATE from macro-event blackout days
    (FOMC/CPI/NFP) — those are a deferred strategy/risk decision, not scheduling.
    """

    session_open: time        # ET — when the supervisor starts the day's session
    session_close: time       # ET — regular full-day close
    half_day_close: time      # ET — early close on a half-day
    holidays: frozenset[date]   # full-closure dates (no session)
    half_days: frozenset[date]  # early-close dates (session ends at half_day_close)
    # Session-open login resilience. In the timer-driven model, cert warmup
    # failures at the open were absorbed by systemd restart tuning; the always-on
    # process no longer exits on a failed login, so the retry moves in-app. BAN
    # RISK is the binding constraint — each failed OAuth refresh accrues toward
    # tastytrade's undocumented IP-block threshold — so this mirrors the retired
    # unit (a handful of attempts, ~10 min apart) rather than a tight loop. A flat
    # interval (not exponential backoff) is deliberate: backing off *faster* early
    # would mean MORE attempts in the ban-counting window, the opposite of safe.
    login_attempts: int
    login_retry_interval_s: float


@dataclass(frozen=True)
class BrokerConfig:
    """Broker HTTP client settings, and WHICH ACCOUNT they address.

    `http_timeout_s` exists because the alternative is an INHERITED default
    nobody chose: the tastytrade SDK builds its httpx client with no timeout
    argument, so every REST call silently used httpx's 5-second default. Five
    seconds is a fine number for a healthy API and a poor one for tastytrade's
    cert environment, which is documented in this very config
    (`staleness.account_refresh_timeout_s`) as returning 502s and hanging.

    On 2026-07-23 the droplet's first shadow run died on
    `GET /customers/me/accounts` after exactly that 5s — a cold, first-ever
    authenticated call from a new IP to the cert API. The point of this key is
    not "make it bigger"; it is to make the bound a deliberate, auditable
    number like every other timing value in this file.

    This is the client-wide DEFAULT. Callers that need a tighter bound still
    impose their own — the account liveness refresher spends
    `account_refresh_timeout_s` as a per-request deadline on its OWN sync client
    (2026-08-26), so this value is only what an unbounded request there would
    inherit. It used to be an `asyncio.timeout` wrapper on the loop; that could
    not bound work on a worker thread, and firing it abandoned the thread.
    → docs/decisions/2026-08-26-account-refresh-off-the-event-loop.md

    `account_number` names the tastytrade account this process trades. `None`
    means "take the login's first account", which is what every environment did
    unconditionally until 2026-09-10 and what dev and sandbox still do. It is
    REQUIRED under `environment = "live"` and the loader refuses without it,
    because every account-derived rail Argus has reads a WHOLE-ACCOUNT number and
    treats it as an Argus-only number — so the live account must be dedicated to
    Argus, and a dedicated account that is merely *assumed* rather than *named* is
    one re-ordered `GET /customers/me/accounts` away from not being the one.
    → docs/decisions/2026-08-31-live-account-is-not-dedicated.md
    """

    http_timeout_s: float
    account_number: str | None = None


@dataclass(frozen=True)
class ReconcileConfig:
    """How long the broker gets to answer the reconciliation question, and for how long.

    Three bounds, nested, and they answer three different questions. Getting them
    confused is how a 30-second blip becomes a five-hour outage or a permanent
    misconfiguration becomes a silent retry loop.

    `request_timeout_s` — **one HTTP attempt.** Deliberately NOT
    `broker.http_timeout_s`, which is the client-wide default sized on 2026-07-23
    for a cold, first-ever authenticated call from a new IP. By the time the
    reconcile fetches run they are the second and third authenticated calls of
    the process, so that allowance has been spent. Anchored at twice the
    already-tuned `staleness.account_refresh_timeout_s`: a once-per-session
    reconcile can afford more patience than a 10s-cadence liveness refresh, and
    needs far less than a cold start. This bound only bites when the broker
    ACCEPTS a socket and never answers — a 502 or a refused connection comes back
    in milliseconds, which is what 2026-08-26 was at process start.

    `retry_interval_s` / `retry_budget_s` — the OUTER loop, which is what makes an
    unreachable broker survivable rather than terminal. Inside one attempt there
    is already a 3-attempt burst with `min(2·n, 5)` backoff (the `_fetch_accounts`
    cadence); this is the slower loop around the whole thing. While it runs the
    process is alive, bound, serving /status and /kill, and **not halted** — the
    scheduler has not started, so there is nothing to un-halt when it succeeds.
    Only the exhausted budget halts.

    30 minutes at 60-second cadence: long enough for a cert outage of realistic
    length (2026-08-26's was ~33 seconds) and short enough that a permanently
    broken deployment reaches a loud terminal state the same morning instead of
    retrying into the weekend.

    **These bound "could not ask" ONLY.** A broker that answered and disagreed is
    a `ReconciliationResult` — a value, not an exception — so it can never reach
    this loop, and a wrong account number raises `SessionAborted` and halts at
    once rather than spending 30 minutes re-proving a permanent fact.
    → docs/designs/2026-08-26-reconcile-broker-unreachable.md
    """

    request_timeout_s: float
    retry_interval_s: float
    retry_budget_s: float


@dataclass(frozen=True)
class MarketDataConfig:
    """Where MARKET DATA comes from — deliberately independent of where ORDERS go.

    Sandbox quotes are 15-minute delayed: fine for validating mechanics, useless
    for judging signal quality, because decisions would be made on stale prices.
    Setting `source = "live"` reads real-time quotes and instruments from the
    production API **read-only** while orders continue to route per `environment`
    (i.e. cert). Reading live market data is NOT trading live — no orders, no
    positions, no money at risk — but it is a different API boundary, so it is an
    explicit opt-in and needs its own live credentials (cert and production are
    separate worlds with separate logins).

    The session layer enforces the split: a market-data session physically
    refuses to be handed to account or order code.
    """

    source: str   # "sandbox" | "live"


@dataclass(frozen=True)
class ServerConfig:
    """The authenticated HTTP control surface's bind address (spec §6 kill switch).

    The kill switch must be "reachable in one action, from a phone, at any time"
    — so the supervisor runs a small always-on HTTP server exposing exactly one
    route, `POST /kill`. This is that server's bind address, and it is
    security-critical: the endpoint trips trading to a halt, so it must be
    reachable ONLY over the private Tailscale interface, never the public
    internet.

    `bind_host` is therefore the droplet's Tailscale IP (100.64.0.0/10) — a
    concrete interface address, never a wildcard. `0.0.0.0`/`::`/empty (bind to
    every interface, including the public one) are REFUSED at load: a
    misconfiguration here would expose the halt control to the internet, so it
    fails closed rather than silently binding wide. If the configured address is
    not actually present on the host at startup, the bind fails loud and the
    process refuses to start — it never falls back to a more exposed address.

    The bearer token that authenticates callers is NOT here — it is a secret and
    comes from the environment (`ARGUS_KILL_TOKEN`), same as the broker
    credentials. `bind_port = 0` asks the OS for an ephemeral port (used by the
    tests); production pins a fixed port so the phone client's URL is stable.
    """

    bind_host: str
    bind_port: int


@dataclass(frozen=True)
class GcConfig:
    """Garbage-collector tuning. OPTIONAL — absent means the shipped defaults.

    Added 2026-08-05, after the intra-session lag ramp was traced to gen-2 GC
    (CLAUDE.md). These are the two levers `app.tune_gc` applies, and they are
    config rather than constants for one operational reason: `argus.toml` is
    BIND-MOUNTED, so changing them is a restart, not an image rebuild. While the
    underlying accumulation is still unfound, being able to back this out — or
    tune it harder — in one restart is worth more than the tidiness of a
    module constant.

    The whole section is OPTIONAL and every key defaults, deliberately. A new
    REQUIRED key would refuse to start against a pre-existing `argus.toml`, which
    is the deploy trap `[watchdog]` and `[risk]` already document. GC tuning is a
    performance knob; it must never be the reason a trading day does not begin.

    To restore stock CPython behaviour without a rebuild:
        [gc]
        freeze_on_startup = false
        gen2_threshold = 10
    """

    #: `gc.freeze()` after startup — moves the startup heap (interpreter, modules,
    #: config, code objects) into a generation gen-2 never rescans. Measured: a
    #: 197ms gen-2 pause over a 2M-object heap dropped to 0.0ms.
    freeze_on_startup: bool = True
    #: gen-2 threshold multiplier. CPython's default is 10 -> a gen-2 pass every
    #: ~70,000 net container allocations, which at this process's ~10k
    #: allocations/sec is every ~7 seconds. 100 makes it ~once a minute. gen-0 and
    #: gen-1 are deliberately NOT configurable: they are cheap, they collect the
    #: short-lived quote churn that genuinely needs collecting, and slowing them
    #: would grow the heap for no benefit.
    gen2_threshold: int = 100


@dataclass(frozen=True)
class Config:
    environment: str
    broker: BrokerConfig
    reconcile: ReconcileConfig
    marketdata: MarketDataConfig
    coa: COAConfig
    risk: RiskConfig
    staleness: StalenessConfig
    audit: AuditConfig
    heartbeat: HeartbeatConfig
    signal: SignalConfig
    execution: ExecutionConfig
    watchdog: WatchdogConfig
    chain: ChainConfig
    market: MarketConfig
    server: ServerConfig
    # OPTIONAL sections, and the two are optional for DIFFERENT reasons.
    #
    # `regime` absent (the shipped state) => the skew side-gate and the size
    # scaler are no-ops. The regime is still computed and logged every tick; what
    # is missing is any threshold to act on, which is the whole point until the
    # telemetry has run long enough to calibrate one.
    #
    # `events` absent => no blackout file, empty date set, nothing suppressed.
    # It ships PRESENT and enabled; absent is the pre-upgrade state, kept legal so
    # code and TOML can deploy in either order.
    #
    # Both are all-or-nothing: a present section is fully validated with no
    # per-key defaults. A half-specified regime gate is worse than no gate.
    regime: RegimeConfig | None = None
    #: GC tuning. Absent section => GcConfig() defaults (see the class).
    gc: GcConfig = field(default_factory=GcConfig)
    events: EventConfig | None = None

    #: Which file this config was read from, and its fingerprint. `None` only for
    #: a `from_dict` construction (tests, and the regime/events parsers) — every
    #: `load` attaches one. See `ConfigSource`.
    source: ConfigSource | None = None

    @staticmethod
    def load(path: str | os.PathLike[str] | None = None) -> "Config":
        """Load and validate config from a TOML file.

        `path` omitted resolves it from the environment — see
        `resolve_config_path`. There is deliberately NO default path: a bare
        `python -m argus` from the repo root used to open `argus.toml`, which is
        the live database, and that default IS the mechanism of the 2026-07-21
        incident. It now refuses.

        The PROFILE is derived from the filename (`argus.<profile>.toml`) and
        checked against the file's own `environment` key by `from_dict`. A path
        that does not match that shape has no profile and gets no such check,
        which is what keeps an arbitrary `--config /tmp/scratch.toml` working.
        """
        if path is None:
            p, _ = resolve_config_path()
        else:
            p = Path(path)
        try:
            text = p.read_bytes()
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {p}") from exc
        except OSError as exc:
            # `IsADirectoryError` specifically, and it is not hypothetical: a bind
            # mount whose HOST path does not exist creates a DIRECTORY at the
            # container path, so `ARGUS_ENV` naming a profile with no
            # `argus.<profile>.toml` beside the compose file arrives here rather
            # than at the branch above. Same fail-closed sentence either way —
            # the loader's refusals are the process's, and a bare OSError
            # traceback reads as a crash rather than as a misconfiguration.
            raise ConfigError(f"config file is not readable: {p}: {exc}") from exc
        try:
            raw = tomllib.loads(text.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config file is not valid TOML: {p}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(f"config file is not valid UTF-8: {p}: {exc}") from exc
        profile = profile_from_filename(p.name)
        config = Config.from_dict(raw, profile=profile)
        # Attached here rather than parsed out of `raw`, because the fingerprint is
        # over the BYTES on disk — the question it answers is "are the bytes I
        # deployed the bytes that are loaded?", and a value re-serialized from the
        # parsed dict would answer a weaker one.
        return replace(
            config,
            source=ConfigSource(
                profile=profile,
                filename=p.name,
                sha=hashlib.sha256(text).hexdigest()[:CONFIG_SHA_LEN],
            ),
        )

    @staticmethod
    def from_dict(raw: dict[str, Any], *, profile: str | None = None) -> "Config":
        environment = _require(raw, "environment", str)
        if environment not in VALID_ENVIRONMENTS:
            raise ConfigError(
                f"environment must be one of {VALID_ENVIRONMENTS}, got {environment!r}"
            )
        _check_profile(profile, environment)

        broker_raw = _require_section(raw, "broker")
        # OPTIONAL in the loader, deliberately, even though all three shipped
        # profiles carry the key (test_config_profiles pins that). A config built
        # by `from_dict` — every test, every tool — must not have to name an
        # account to construct a broker section, and "" is how dev and sandbox
        # SAY unpinned in a file whose key set has to match live's.
        account_raw = broker_raw.get("account_number", "")
        if not isinstance(account_raw, str):
            raise ConfigError(
                "config key broker.account_number must be a string "
                f"(got {type(account_raw).__name__}); tastytrade account numbers are "
                "not integers — a bare 5XX00000 is not valid TOML and a quoted one is"
            )
        account_number = account_raw.strip() or None
        if account_number is not None and any(c.isspace() for c in account_number):
            # A typo'd number does not fail here today — it fails at the account
            # filter as SessionAborted, which is PERMANENT and parks the process.
            # Refusing at load turns a parked droplet into a failed start.
            raise ConfigError(
                f"config key broker.account_number must not contain whitespace, got "
                f"{account_number!r}: a mistyped account number is not rejected by the "
                "broker, it simply matches nothing, and the account filter answers that "
                "with a PERMANENT SessionAborted"
            )
        if environment == "live" and account_number is None:
            # Its OWN branch, not shared with the `account_stream_max_age_s` or
            # `audit.synchronous` live guards below: the three refuse live for
            # three unrelated reasons, and sharing a guard is precisely the
            # mistake the staleness one's own comment warns against. This one is
            # parsed first only because `[broker]` is — the guard belongs beside
            # its key.
            raise ConfigError(
                "environment='live' requires broker.account_number: Argus's "
                "account-derived rails (reconciliation's whole-account position map, "
                "the concurrency cap's leg count, the daily-loss breaker's NLV anchor "
                "and the relative size rail) all read WHOLE-ACCOUNT numbers and treat "
                "them as Argus-only, so the traded account must be DEDICATED to Argus "
                "and must be NAMED rather than inherited from whichever account the "
                "login lists first. See "
                "docs/decisions/2026-08-31-live-account-is-not-dedicated.md"
            )
        broker = BrokerConfig(
            http_timeout_s=_positive_float(broker_raw, "http_timeout_s", "broker"),
            account_number=account_number,
        )

        rec_raw = _require_section(raw, "reconcile")
        reconcile = ReconcileConfig(
            request_timeout_s=_positive_float(rec_raw, "request_timeout_s", "reconcile"),
            retry_interval_s=_positive_float(rec_raw, "retry_interval_s", "reconcile"),
            retry_budget_s=_positive_float(rec_raw, "retry_budget_s", "reconcile"),
        )
        if reconcile.retry_budget_s < reconcile.retry_interval_s:
            # A budget smaller than one interval means exactly one attempt, which
            # is the no-outer-loop behaviour spelled in a way that reads like an
            # outer loop. Refuse rather than silently ship the old semantics.
            raise ConfigError(
                "config key reconcile.retry_budget_s must be >= reconcile.retry_interval_s "
                f"({reconcile.retry_budget_s:g} < {reconcile.retry_interval_s:g}); a budget "
                "shorter than one interval disables the retry it looks like it configures"
            )

        md_raw = _require_section(raw, "marketdata")
        source = _require(md_raw, "source", str, section="marketdata")
        if source not in VALID_MARKETDATA_SOURCES:
            raise ConfigError(
                f"marketdata.source must be one of {VALID_MARKETDATA_SOURCES}, got {source!r}"
            )
        marketdata = MarketDataConfig(source=source)

        coa_raw = _require_section(raw, "coa")
        coa = COAConfig(
            response_window_ms=_positive_int(coa_raw, "response_window_ms", "coa"),
            extended_processing_ms=_positive_int(coa_raw, "extended_processing_ms", "coa"),
            source=_require(coa_raw, "source", str, section="coa"),
        )

        risk_raw = _require_section(raw, "risk")
        daily_max_loss = _require(risk_raw, "daily_max_loss", (int, float), section="risk")
        if daily_max_loss <= 0:
            raise ConfigError("risk.daily_max_loss must be > 0")
        position_size_pct = _require(risk_raw, "position_size_pct", (int, float), section="risk")
        if not 0 < position_size_pct <= 1:
            raise ConfigError("risk.position_size_pct must be in (0, 1]")
        max_loss_per_position = _positive_float(risk_raw, "max_loss_per_position", "risk")
        if max_loss_per_position > daily_max_loss:
            # A per-trade cap above the day's cap cannot bind before the breaker
            # does, which makes it decoration rather than a rail. Refuse rather
            # than ship a limit that can never fire.
            raise ConfigError(
                f"risk.max_loss_per_position ({max_loss_per_position:g}) must be <= "
                f"risk.daily_max_loss ({daily_max_loss:g}) — a single position may not "
                "be allowed to risk more than the whole day's loss limit"
            )
        risk = RiskConfig(
            daily_max_loss=float(daily_max_loss),
            max_concurrent_positions=_positive_int(risk_raw, "max_concurrent_positions", "risk"),
            position_size_pct=float(position_size_pct),
            max_loss_per_position=max_loss_per_position,
            max_contracts_per_position=_positive_int(
                risk_raw, "max_contracts_per_position", "risk"
            ),
            # REQUIRED and strictly a bool, on `profit_target_enabled`'s precedent: a
            # hand-edited `side_balance = 1` or `"false"` (truthy) is a ConfigError at
            # boot rather than an entry rule nobody chose.
            side_balance=_require(risk_raw, "side_balance", bool, section="risk"),
            max_side_imbalance=_positive_int(risk_raw, "max_side_imbalance", "risk"),
        )

        stale_raw = _require_section(raw, "staleness")
        account_max_age = _positive_float(stale_raw, "account_stream_max_age_s", "staleness")
        if environment == "live" and account_max_age > LIVE_MAX_ACCOUNT_STREAM_AGE_S:
            raise ConfigError(
                f"environment='live' requires staleness.account_stream_max_age_s <= "
                f"{LIVE_MAX_ACCOUNT_STREAM_AGE_S}, got {account_max_age}: values above it "
                f"exist only to tolerate the tastytrade SANDBOX's /positions latency, and "
                f"in live this rail is what stops an entry being sized and gated against "
                f"an account picture nothing has proved is current"
            )
        account_refresh = _positive_float(stale_raw, "account_refresh_s", "staleness")
        if account_refresh >= account_max_age:
            raise ConfigError(
                "staleness.account_refresh_s must be < staleness.account_stream_max_age_s, "
                "or the account cache goes stale between refreshes and the gate blocks "
                "every entry"
            )
        refresh_timeout = _positive_float(stale_raw, "account_refresh_timeout_s", "staleness")
        if refresh_timeout >= account_refresh:
            raise ConfigError(
                "staleness.account_refresh_timeout_s must be < staleness.account_refresh_s, "
                "or a single hung request outlasts the refresh interval and the retry "
                "never gets a turn"
            )
        evict_after = _positive_float(stale_raw, "account_position_evict_after_s", "staleness")
        if evict_after <= account_refresh:
            raise ConfigError(
                "staleness.account_position_evict_after_s must be > "
                "staleness.account_refresh_s, or a position the broker really holds "
                "can age out between two seeds and be evicted on timing jitter — "
                "which under-counts open spreads and releases a concurrent slot"
            )
        market_max_age = _positive_float(stale_raw, "market_data_max_age_s", "staleness")
        greeks_max_age = _positive_float(stale_raw, "greeks_max_age_s", "staleness")
        if greeks_max_age < market_max_age:
            # Not an arbitrary ordering. A leg is ADMITTED on its quote and only
            # then considered for the short role, so a greeks bound tighter than
            # the quote bound cannot admit anything the quote bound would not have
            # admitted already — it would just be `market_data_max_age_s` wearing a
            # second name, and the role split (2026-08-24) would be undone by
            # config rather than by code.
            raise ConfigError(
                "staleness.greeks_max_age_s must be >= staleness.market_data_max_age_s "
                f"(got {greeks_max_age} vs {market_max_age}): a leg is admitted on its "
                "QUOTE and only then considered as a short strike, so a tighter greeks "
                "bound selects nothing the quote bound has not already refused"
            )
        staleness = StalenessConfig(
            market_data_max_age_s=market_max_age,
            greeks_max_age_s=greeks_max_age,
            account_stream_max_age_s=account_max_age,
            account_refresh_s=account_refresh,
            account_refresh_timeout_s=refresh_timeout,
            account_position_evict_after_s=evict_after,
        )

        audit_raw = _require_section(raw, "audit")
        maxsize = _positive_int(audit_raw, "tier2_queue_maxsize", "audit")
        alert_depth = _positive_int(audit_raw, "tier2_queue_alert_depth", "audit")
        if alert_depth > maxsize:
            raise ConfigError(
                "audit.tier2_queue_alert_depth must be <= audit.tier2_queue_maxsize"
            )
        synchronous = _require(audit_raw, "synchronous", str, section="audit").upper()
        if synchronous not in VALID_SYNCHRONOUS:
            raise ConfigError(
                f"audit.synchronous must be one of {VALID_SYNCHRONOUS}, got {synchronous!r}"
            )
        if environment == "live" and synchronous not in DURABLE_SYNCHRONOUS:
            # Structural, like sandbox-first: going live must not be able to
            # inherit shadow's weaker durability by omission. Under NORMAL a host
            # crash can roll back the last commits, and the audit log's tail is
            # what reconciliation reads back as expected state on restart — so
            # the failure mode is reconciling against a record of positions that
            # is quietly missing the most recent one.
            raise ConfigError(
                f"environment='live' requires audit.synchronous to be one of "
                f"{DURABLE_SYNCHRONOUS}, got {synchronous!r}: under {synchronous} a "
                f"host-level crash can lose the most recent audit records, which are "
                f"the expected state reconciliation compares against on restart"
            )
        audit = AuditConfig(
            db_path=_require(audit_raw, "db_path", str, section="audit"),
            tier2_queue_maxsize=maxsize,
            tier2_queue_alert_depth=alert_depth,
            synchronous=synchronous,
        )

        hb_raw = _require_section(raw, "heartbeat")
        heartbeat = HeartbeatConfig(
            interval_s=_positive_float(hb_raw, "interval_s", "heartbeat"),
            lag_alert_threshold_s=_positive_float(hb_raw, "lag_alert_threshold_s", "heartbeat"),
            sustained_breach_count=_positive_int(hb_raw, "sustained_breach_count", "heartbeat"),
            trip_on_sustained_stall=_require(
                hb_raw, "trip_on_sustained_stall", bool, section="heartbeat"
            ),
        )

        sig_raw = _require_section(raw, "signal")
        sides = sig_raw.get("sides")
        if (
            not isinstance(sides, list)
            or not sides
            or any(s not in VALID_SIDES for s in sides)
        ):
            raise ConfigError(f"signal.sides must be a non-empty list of {VALID_SIDES}")
        target_delta = _require(sig_raw, "target_short_delta", (int, float), section="signal")
        if not 0 < target_delta < 1:
            raise ConfigError("signal.target_short_delta must be in (0, 1)")
        earliest = _parse_time(_require(sig_raw, "entry_earliest", str, section="signal"),
                               "signal.entry_earliest")
        latest = _parse_time(_require(sig_raw, "entry_latest", str, section="signal"),
                             "signal.entry_latest")
        if earliest >= latest:
            raise ConfigError("signal.entry_earliest must be before signal.entry_latest")
        delta_source = _require(sig_raw, "short_delta_source", str, section="signal")
        if delta_source not in SHORT_DELTA_SOURCES:
            raise ConfigError(
                f"signal.short_delta_source must be one of {SHORT_DELTA_SOURCES}, "
                f"got {delta_source!r}"
            )
        width = _positive_float(sig_raw, "width", "signal")
        max_width = _positive_float(sig_raw, "max_width", "signal")
        if max_width < width:
            raise ConfigError("signal.max_width must be >= signal.width")
        signal = SignalConfig(
            sides=tuple(sides),
            target_short_delta=float(target_delta),
            width=width,
            max_width=max_width,
            min_credit=_positive_float(sig_raw, "min_credit", "signal"),
            entry_earliest=earliest,
            entry_latest=latest,
            cadence_s=_positive_float(sig_raw, "cadence_s", "signal"),
            short_delta_source=delta_source,
        )

        exec_raw = _require_section(raw, "execution")
        signal_max_age = _positive_float(exec_raw, "signal_max_age_s", "execution")
        if signal_max_age > staleness.market_data_max_age_s:
            # A construction bound looser than the feed's own staleness threshold
            # would never bind: the gate would already have blocked the entry.
            # Refuse rather than ship a check that silently does nothing.
            raise ConfigError(
                "execution.signal_max_age_s must be <= staleness.market_data_max_age_s, "
                "or the freshness check at order construction can never fire (the "
                "pre-trade gate blocks on the looser threshold first)"
            )
        walk_away_pct = _require(
            exec_raw, "entry_walk_away_pct", (int, float), section="execution"
        )
        if not 0 < walk_away_pct <= 1:
            raise ConfigError("execution.entry_walk_away_pct must be in (0, 1]")
        walk_source = _require(exec_raw, "walk_source", str, section="execution").strip()
        if not walk_source:
            # Same rule as coa.source. These numbers give away real credit on a
            # live entry; an empty provenance note means nobody has said where they
            # came from, and a placeholder that looks decided is worse than none.
            raise ConfigError(
                "execution.walk_source must be a non-empty provenance note saying "
                "where entry_cadence_s / entry_step_ticks / entry_walk_away_pct / "
                "entry_drift_abort_points came from and when to revisit them"
            )
        poll_interval = _positive_float(exec_raw, "status_poll_interval_s", "execution")
        working_timeout = _positive_float(exec_raw, "working_timeout_s", "execution")
        if poll_interval > working_timeout:
            # A poll slower than the timeout it is measuring gets one look before
            # giving up, which makes the timeout meaningless.
            raise ConfigError(
                "execution.status_poll_interval_s must be <= execution.working_timeout_s, "
                "or an order gets a single status check before being cancelled"
            )
        guard_timeout = _positive_float(exec_raw, "guard_timeout_s", "execution")
        if guard_timeout > broker.http_timeout_s:
            # The SDK's own client timeout fires first, so a looser bound here is a
            # check that can never bind — the same "refuse a rail that silently does
            # nothing" rule signal_max_age_s is held to above.
            raise ConfigError(
                "execution.guard_timeout_s must be <= broker.http_timeout_s, or the "
                "SDK's own client timeout fires first and this bound never binds"
            )
        # The ONE defaulting key in this loader, and it defaults to the safe side:
        # absent means dry-run, so a config that predates chunk 5 cannot start
        # routing real orders the moment `environment` is flipped. A present value
        # must still be a real bool — `dry_run_submissions = "false"` is a config
        # error, not a truthy string.
        dry_run = exec_raw.get("dry_run_submissions", True)
        if not isinstance(dry_run, bool):
            raise ConfigError(
                "execution.dry_run_submissions must be a boolean (true/false), got "
                f"{dry_run!r}"
            )
        execution = ExecutionConfig(
            signal_max_age_s=signal_max_age,
            entry_cadence_s=_positive_float(exec_raw, "entry_cadence_s", "execution"),
            entry_step_ticks=_positive_int(exec_raw, "entry_step_ticks", "execution"),
            entry_walk_away_pct=float(walk_away_pct),
            # Required, not defaulted. A number here abandons live entries, so there
            # is no value this loader could pick that would not be a guess with real
            # consequences — the same reason the three above are required. It is
            # `walk_source`'s job to say the shipped number is still a placeholder.
            entry_drift_abort_points=_positive_float(
                exec_raw, "entry_drift_abort_points", "execution"
            ),
            walk_source=walk_source,
            working_timeout_s=working_timeout,
            status_poll_interval_s=poll_interval,
            max_recovery_attempts=_positive_int(exec_raw, "max_recovery_attempts", "execution"),
            max_order_requests_per_second=_positive_float(
                exec_raw, "max_order_requests_per_second", "execution"
            ),
            guard_timeout_s=guard_timeout,
            guard_unreachable_max_streak=_positive_int(
                exec_raw, "guard_unreachable_max_streak", "execution"
            ),
            dry_run_submissions=dry_run,
        )

        wd_raw = _require_section(raw, "watchdog")
        wd_cadence = _positive_float(wd_raw, "cadence_s", "watchdog")
        refresh_interval = _positive_float(wd_raw, "refresh_interval_s", "watchdog")
        if refresh_interval < wd_cadence:
            # A ledger re-read per evaluation would put an automated hot path on the
            # audit log's writer-connection lock, which `AuditLog.read` explicitly
            # forbids ("reads are RARE — a manual phone reload, never an automated
            # hot path"). Refuse the configuration rather than ship the contention.
            raise ConfigError(
                "watchdog.refresh_interval_s must be >= watchdog.cadence_s: the "
                "position-ledger read shares the audit log's writer lock with "
                "crash-critical Tier 1 writes and must not run every evaluation"
            )
        book_settle = _require(wd_raw, "book_settle_s", (int, float), section="watchdog")
        if book_settle < 0:
            # 0 is valid and means "page on the first observation", i.e. the
            # pre-2026-08-13 behaviour. Negative is not a shorter allowance, it is
            # a nonsense one.
            raise ConfigError("watchdog.book_settle_s must be >= 0")
        book_confirm_ttl = _positive_float(wd_raw, "book_confirm_ttl_s", "watchdog")
        if book_confirm_ttl <= refresh_interval:
            # The book confirm is awaited inside a `check_once` pass, so every
            # round trip is a blind window of up to
            # `staleness.account_refresh_timeout_s` with no position evaluated
            # against its triggers. What keeps that survivable is that a steady
            # disagreement confirms ONCE, not once per observation — and
            # `_check_broker_agreement` runs from `_refresh_if_due`, i.e. at most
            # every `refresh_interval_s`. A TTL at or below that cadence expires
            # every entry before it can ever be hit again, which restores a round
            # trip per observation and with it the frequency this cache exists to
            # bound.
            # → docs/decisions/2026-08-21-book-confirm-cache-is-per-shape-and-expires.md
            raise ConfigError(
                "watchdog.book_confirm_ttl_s must be > watchdog.refresh_interval_s "
                f"(got {book_confirm_ttl} vs {refresh_interval}): the book confirm "
                "is checked no more often than the refresh cadence, so a TTL at or "
                "below it means every verdict expires before it can be reused — a "
                "REST round trip, and its blind window, on every observation"
            )
        credit_multiple = _require(
            wd_raw, "credit_multiple", (int, float), section="watchdog"
        )
        if credit_multiple <= 1:
            raise ConfigError(
                f"watchdog.credit_multiple must be > 1 (got {credit_multiple}): at 1.0 the "
                "cost to close equals the credit received, so the trigger fires at "
                "break-even — an immediate exit, not a stop"
            )
        buffer_points = _require(
            wd_raw, "underlying_buffer_points", (int, float), section="watchdog"
        )
        if buffer_points < 0:
            # 0 is valid ("trigger when the strike is touched"); negative would mean
            # waiting until the short strike is already breached by a margin.
            raise ConfigError("watchdog.underlying_buffer_points must be >= 0")
        sustained = _positive_float(wd_raw, "sustained_s", "watchdog")
        if sustained < wd_cadence:
            raise ConfigError(
                "watchdog.sustained_s must be >= watchdog.cadence_s, or a single "
                "observation satisfies the debounce and a momentary spread blowout "
                "triggers an exit (spec §7: sustained for several consecutive seconds)"
            )
        exit_walk_away_pct = _require(
            wd_raw, "exit_walk_away_pct", (int, float), section="watchdog"
        )
        if not 0 < exit_walk_away_pct <= 1:
            raise ConfigError("watchdog.exit_walk_away_pct must be in (0, 1]")
        unmanaged_order_max = _positive_float(
            wd_raw, "unmanaged_order_max_s", "watchdog"
        )
        if unmanaged_order_max <= refresh_interval:
            # The recheck runs from `_refresh_if_due`, i.e. at most every
            # `refresh_interval_s`. A bound at or below that cadence means every
            # interval has already elapsed by the next observation, so the page
            # fires on every poll and the re-cancel runs at the poll's rate — the
            # alert storm `_Escalation.overdue` exists to bound, rebuilt out of its
            # own configuration.
            # → docs/decisions/2026-08-29-mid-session-cancel-recheck.md
            raise ConfigError(
                "watchdog.unmanaged_order_max_s must be > watchdog.refresh_interval_s "
                f"(got {unmanaged_order_max} vs {refresh_interval}): the residual-order "
                "recheck is driven by the refresh cadence, so a bound at or below it "
                "pages and re-cancels on every poll"
            )
        # The SECOND defaulting key in this loader, and it defaults to the safe side
        # exactly as `execution.dry_run_submissions` does: absent means `would_cancel`
        # (decide and record, send nothing), so an image carrying P5 cannot start
        # cancelling orders against a config written before it. A present value must
        # still be a real bool.
        unmanaged_cancel = wd_raw.get("unmanaged_order_cancel", False)
        if not isinstance(unmanaged_cancel, bool):
            raise ConfigError(
                "watchdog.unmanaged_order_cancel must be a boolean (true/false), got "
                f"{unmanaged_cancel!r}"
            )
        # REQUIRED and strictly a bool: `_require` refuses an int or a string here, so
        # a hand-edited `profit_target_enabled = 1` or `"false"` (truthy) is a
        # ConfigError at boot rather than a live behaviour nobody chose.
        profit_target_enabled = _require(
            wd_raw, "profit_target_enabled", bool, section="watchdog"
        )
        # REQUIRED and strictly a bool, same precedent: it switches the dominant
        # emergency exit off, so a truthy typo must not decide it.
        credit_trigger_enabled = _require(
            wd_raw, "credit_trigger_enabled", bool, section="watchdog"
        )
        # REQUIRED and strictly a bool, on `profit_target_enabled`'s precedent:
        # a hand-edited `stub_close_enabled = 1` or `"true"` (truthy) is a
        # ConfigError at boot rather than a live behaviour nobody chose.
        stub_close_enabled = _require(
            wd_raw, "stub_close_enabled", bool, section="watchdog"
        )
        trigger_source = _require(
            wd_raw, "trigger_source", str, section="watchdog"
        ).strip()
        if not trigger_source:
            # Same rule as coa.source and execution.walk_source. These numbers
            # decide when a live losing position is closed; an empty provenance
            # note means nobody has said where they came from.
            raise ConfigError(
                "watchdog.trigger_source must be a non-empty provenance note saying "
                "where credit_multiple / underlying_buffer_points / sustained_s came "
                "from and when to revisit them"
            )
        # The flatten deferral (2026-09-25). Five REQUIRED keys, each validated here so
        # a hand-edited value is a boot refusal rather than a behaviour nobody chose.
        flatten_deferral = _require(
            wd_raw, "flatten_deferral", str, section="watchdog"
        ).strip()
        if flatten_deferral not in FLATTEN_DEFERRAL_MODES:
            raise ConfigError(
                f"watchdog.flatten_deferral must be one of "
                f"{sorted(FLATTEN_DEFERRAL_MODES)}, got {flatten_deferral!r}"
            )
        defer_k = _finite_float(wd_raw, "flatten_defer_insurance_multiple", "watchdog")
        if defer_k < 1.0:
            # Below 1 the rule declines the insurance even when it is UNDERPRICED —
            # a sign error, not a risk appetite.
            raise ConfigError(
                "watchdog.flatten_defer_insurance_multiple must be >= 1 (k < 1 would "
                f"skip a flatten whose insurance is worth buying), got {defer_k:g}"
            )
        defer_z99 = _finite_float(wd_raw, "flatten_defer_tail_z99", "watchdog")
        if defer_z99 <= 0.0:
            raise ConfigError("watchdog.flatten_defer_tail_z99 must be > 0")
        defer_alpha = _finite_float(wd_raw, "flatten_defer_tail_alpha", "watchdog")
        if not 1.0 < defer_alpha <= MAX_FLATTEN_DEFER_TAIL_ALPHA:
            raise ConfigError(
                f"watchdog.flatten_defer_tail_alpha must be in (1, "
                f"{MAX_FLATTEN_DEFER_TAIL_ALPHA:g}], got {defer_alpha:g}: the recorded "
                "sample cannot certify a tail thinner than the inverse cubic law"
            )
        defer_source = _require(
            wd_raw, "flatten_defer_source", str, section="watchdog"
        ).strip()
        if not defer_source:
            raise ConfigError(
                "watchdog.flatten_defer_source must be a non-empty provenance note "
                "saying where k, z99 and alpha came from"
            )
        if flatten_deferral == "on" and not risk.open_risk_is_welded:
            # The deferral's test is PER POSITION, and it is valid for any number of
            # positions in EXPECTATION (expected losses add, correlated or not). What a
            # second position changes is CONCENTRATION: same-side spreads are touched
            # by one move, so one barrier event can cost several positions at once.
            # The quantity to bound is therefore the DOLLARS one event can cost across
            # every deferred position — at most `max_concurrent_positions ×
            # max_loss_per_position` — and the welded invariant holds it to
            # `daily_max_loss`, the same bound the approved one-position configuration
            # had (1 × 950). Keyed on the dollars, never on the COUNT: 2 spreads × 1
            # contract at 475 each is the same worst event as 1 × 2 at 950, and the
            # first cut's `max_concurrent_positions == 1` refused exactly that move.
            # → docs/decisions/2026-09-25-deferring-a-far-otm-flatten.md
            raise ConfigError(
                "watchdog.flatten_deferral = \"on\" requires the welded size invariant "
                "risk.max_concurrent_positions x risk.max_loss_per_position <= "
                f"risk.daily_max_loss (got {risk.max_concurrent_positions} x "
                f"{risk.max_loss_per_position:g} = "
                f"{risk.max_concurrent_positions * risk.max_loss_per_position:g} > "
                f"{risk.daily_max_loss:g}): one barrier event could cost the deferred "
                "positions more than the day's loss budget"
            )
        watchdog = WatchdogConfig(
            cadence_s=wd_cadence,
            refresh_interval_s=refresh_interval,
            book_settle_s=float(book_settle),
            book_confirm_ttl_s=book_confirm_ttl,
            credit_multiple=float(credit_multiple),
            underlying_buffer_points=float(buffer_points),
            sustained_s=sustained,
            exit_cadence_s=_positive_float(wd_raw, "exit_cadence_s", "watchdog"),
            exit_step_ticks=_positive_int(wd_raw, "exit_step_ticks", "watchdog"),
            exit_walk_away_pct=float(exit_walk_away_pct),
            # `_positive_int`, so 0 is refused with the negatives: 0 is exactly the
            # behaviour this key exists to end (2026-08-31). A walk with no reprice in
            # it is not a walk — it offers the mid once and escalates to the unguarded
            # market cross, which on 2026-08-31 was the whole of both flattens.
            exit_walk_min_steps=_positive_int(wd_raw, "exit_walk_min_steps", "watchdog"),
            exit_cancel_settle_s=_positive_float(
                wd_raw, "exit_cancel_settle_s", "watchdog"
            ),
            # `_positive_float`, so 0 is refused along with negatives. Unlike
            # `book_settle_s` — where 0 restores an older, stricter behaviour — 0
            # here would mean "page on the tick after the first one", i.e. two pages
            # for one condition, which is the alert-storm shape and not an escape
            # hatch anybody wants.
            exit_unattempted_max_s=_positive_float(
                wd_raw, "exit_unattempted_max_s", "watchdog"
            ),
            # `> 0` here; the cross-section bound against the shortest possible
            # session is checked below, once `[market]` has been parsed.
            flatten_before_close_s=_positive_float(
                wd_raw, "flatten_before_close_s", "watchdog"
            ),
            unmanaged_order_max_s=unmanaged_order_max,
            unmanaged_order_cancel=unmanaged_cancel,
            profit_target_enabled=profit_target_enabled,
            credit_trigger_enabled=credit_trigger_enabled,
            stub_close_enabled=stub_close_enabled,
            trigger_source=trigger_source,
            flatten_deferral=flatten_deferral,
            flatten_defer_insurance_multiple=defer_k,
            flatten_defer_tail_z99=defer_z99,
            flatten_defer_tail_alpha=defer_alpha,
            flatten_defer_source=defer_source,
        )

        chain_raw = _require_section(raw, "chain")
        chain = ChainConfig(
            underlying_symbol=_require(chain_raw, "underlying_symbol", str, section="chain"),
            chain_symbol=_require(chain_raw, "chain_symbol", str, section="chain"),
            strike_window_points=_positive_float(chain_raw, "strike_window_points", "chain"),
        )

        market_raw = _require_section(raw, "market")
        session_open = _parse_time(
            _require(market_raw, "session_open", str, section="market"), "market.session_open"
        )
        session_close = _parse_time(
            _require(market_raw, "session_close", str, section="market"), "market.session_close"
        )
        half_day_close = _parse_time(
            _require(market_raw, "half_day_close", str, section="market"), "market.half_day_close"
        )
        if session_open >= session_close:
            raise ConfigError("market.session_open must be before market.session_close")
        if session_open >= half_day_close:
            # A half-day whose early close precedes the session open would never
            # run a session — fail closed on the misconfiguration rather than
            # silently skipping every half-day.
            raise ConfigError("market.session_open must be before market.half_day_close")
        # CROSS-SECTION, and it has to live here because `[watchdog]` is parsed
        # first: the flatten mark is an OFFSET from that day's close, so the shortest
        # session the calendar can produce is what bounds it. A half-day is
        # `session_open` -> `half_day_close`; an offset at or beyond that length puts
        # the mark before the open, and every position on that day is born triggered
        # — the one way this control can do harm rather than merely fail to fire.
        shortest_session_s = _seconds_between(session_open, half_day_close)
        if watchdog.flatten_before_close_s >= shortest_session_s:
            raise ConfigError(
                f"watchdog.flatten_before_close_s ({watchdog.flatten_before_close_s:g}) "
                f"must be less than the shortest session the calendar can produce "
                f"({shortest_session_s:g}s, market.session_open -> "
                f"market.half_day_close): the flatten mark would fall before the open "
                f"on a half-day and every position would be triggered on entry"
            )
        market = MarketConfig(
            session_open=session_open,
            session_close=session_close,
            half_day_close=half_day_close,
            holidays=_parse_dates(market_raw, "holidays"),
            half_days=_parse_dates(market_raw, "half_days"),
            login_attempts=_positive_int(market_raw, "login_attempts", "market"),
            login_retry_interval_s=_positive_float(
                market_raw, "login_retry_interval_s", "market"
            ),
        )

        server_raw = _require_section(raw, "server")
        bind_host = _require(server_raw, "bind_host", str, section="server").strip()
        if bind_host in WILDCARD_BIND_HOSTS:
            # Fail closed: a wildcard bind would expose the halt control to the
            # public internet. Refuse rather than silently binding wide.
            raise ConfigError(
                f"server.bind_host must be a concrete private interface address "
                f"(e.g. the Tailscale IP), not a wildcard {bind_host!r}: the kill-switch "
                f"endpoint must never be reachable from the public internet"
            )
        bind_port = _require(server_raw, "bind_port", int, section="server")
        if not 0 <= bind_port <= MAX_PORT:
            raise ConfigError(
                f"server.bind_port must be in [0, {MAX_PORT}] (0 = OS-assigned), got {bind_port}"
            )
        server = ServerConfig(bind_host=bind_host, bind_port=bind_port)

        # Optional sections. Presence — not a per-key default — is the switch, so
        # there is no way to end up half-configured (see the Config fields).
        regime = None
        if "regime" in raw:
            regime = Config._parse_regime(_require_section(raw, "regime"))
        events = None
        if "events" in raw:
            events = Config._parse_events(_require_section(raw, "events"))
        # Unlike [regime]/[events], an absent [gc] means DEFAULTS rather than
        # "feature off": the tuning is on by default and the section only exists to
        # override or disable it. A missing section must never block a deploy.
        gc_cfg = Config._parse_gc(raw.get("gc") or {})

        return Config(
            environment=environment,
            broker=broker,
            reconcile=reconcile,
            marketdata=marketdata,
            coa=coa,
            risk=risk,
            staleness=staleness,
            audit=audit,
            heartbeat=heartbeat,
            signal=signal,
            execution=execution,
            watchdog=watchdog,
            chain=chain,
            market=market,
            server=server,
            regime=regime,
            gc=gc_cfg,
            events=events,
        )

    @staticmethod
    def _parse_gc(raw: dict[str, Any]) -> GcConfig:
        """Parse `[gc]`. Every key defaults; only a PRESENT-but-wrong value fails.

        `freeze_on_startup` must be a real bool — `"false"` is a ConfigError, not
        a truthy string, the same rule `execution.dry_run_submissions` follows.
        A typo'd toggle that silently reads as "on" is the failure mode.
        """
        defaults = GcConfig()
        freeze = raw.get("freeze_on_startup", defaults.freeze_on_startup)
        if not isinstance(freeze, bool):
            raise ConfigError("gc.freeze_on_startup must be a boolean (true/false)")
        threshold = raw.get("gen2_threshold", defaults.gen2_threshold)
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise ConfigError("gc.gen2_threshold must be an integer >= 1 (CPython default is 10)")
        return GcConfig(freeze_on_startup=freeze, gen2_threshold=threshold)

    @staticmethod
    def _parse_regime(regime_raw: dict[str, Any]) -> RegimeConfig:
        iv_low_max = _positive_float(regime_raw, "atm_iv_low_max", "regime")
        iv_high_min = _positive_float(regime_raw, "atm_iv_high_min", "regime")
        if iv_low_max >= iv_high_min:
            # Otherwise the "mid" bucket is empty or inverted, and the three size
            # multipliers no longer describe three distinct states.
            raise ConfigError("regime.atm_iv_low_max must be < regime.atm_iv_high_min")

        multipliers = {
            name: _positive_float(regime_raw, name, "regime")
            for name in ("size_multiplier_low", "size_multiplier_mid", "size_multiplier_high")
        }
        for name, value in multipliers.items():
            if value > 1:
                # This section is a risk-REDUCTION knob. Letting it scale above 1
                # would put a research-motivated, uncalibrated number in a position
                # to inflate size past the rail the operator actually decided.
                raise ConfigError(
                    f"regime.{name} must be <= 1 — this section scales risk down, never up"
                )

        return RegimeConfig(
            atm_iv_low_max=iv_low_max,
            atm_iv_high_min=iv_high_min,
            max_downside_skew_for_puts=_positive_float(
                regime_raw, "max_downside_skew_for_puts", "regime"
            ),
            max_upside_skew_for_calls=_positive_float(
                regime_raw, "max_upside_skew_for_calls", "regime"
            ),
            **multipliers,
        )

    @staticmethod
    def _parse_events(events_raw: dict[str, Any]) -> EventConfig:
        return EventConfig(
            enabled=_require(events_raw, "enabled", bool, section="events"),
            blackout_dates_path=_require(
                events_raw, "blackout_dates_path", str, section="events"
            ),
        )


def _require_section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"missing or malformed [{name}] section")
    return section


def _require(
    d: dict[str, Any],
    key: str,
    types: type | tuple[type, ...],
    *,
    section: str | None = None,
) -> Any:
    where = f"{section}.{key}" if section else key
    if key not in d:
        raise ConfigError(f"missing required config key: {where}")
    value = d[key]
    # bool is a subclass of int — reject it where a number is expected.
    if isinstance(types, type):
        types = (types,)
    if isinstance(value, bool) and bool not in types:
        raise ConfigError(f"config key {where} must be {types}, got bool")
    if not isinstance(value, types):
        raise ConfigError(f"config key {where} must be {types}, got {type(value).__name__}")
    return value


def _positive_int(d: dict[str, Any], key: str, section: str) -> int:
    value = _require(d, key, int, section=section)
    if value <= 0:
        raise ConfigError(f"config key {section}.{key} must be > 0")
    return value


def _positive_float(d: dict[str, Any], key: str, section: str) -> float:
    value = _require(d, key, (int, float), section=section)
    if value <= 0:
        raise ConfigError(f"config key {section}.{key} must be > 0")
    return float(value)


def _finite_float(d: dict[str, Any], key: str, section: str) -> float:
    value = _require(d, key, (int, float), section=section)
    if not math.isfinite(value):
        raise ConfigError(f"config key {section}.{key} must be finite, got {value!r}")
    return float(value)


def _seconds_between(start: time, end: time) -> float:
    """Seconds from `start` to `end` on one day. Callers check the ordering."""
    return (
        datetime.combine(date.min, end) - datetime.combine(date.min, start)
    ).total_seconds()


def _parse_time(value: str, where: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{where} must be HH:MM (24h), got {value!r}") from exc


def _parse_date(value: str, where: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{where} must be an ISO date YYYY-MM-DD, got {value!r}") from exc


def load_blackout_dates(path: str | os.PathLike[str]) -> frozenset[date]:
    """Load the event-blackout date file referenced by `EventConfig.blackout_dates_path`.

    Lives here rather than in a `signal/` module because it is a config-file
    loader, not trading logic — same category as `_parse_dates` above, which does
    the identical job for the market calendar's holiday list.

    Format: one ISO date (YYYY-MM-DD) per line; blank lines and lines starting
    with `#` are ignored, so the generator can annotate each date with the event
    that produced it.

    FAIL-CLOSED, in both directions that matter. A missing file raises rather than
    yielding an empty set: `[events] enabled = true` against no file means the
    operator believes entries are being suppressed when nothing is, and that
    belief is worth more than a session. A malformed line raises rather than being
    skipped, because a silently-dropped FOMC date defeats the entire point.
    """
    p = Path(path)
    try:
        lines = p.read_text().splitlines()
    except FileNotFoundError as exc:
        raise ConfigError(f"blackout dates file not found: {p}") from exc

    dates: set[date] = set()
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            dates.add(date.fromisoformat(line))
        except ValueError as exc:
            raise ConfigError(f"{p}:{lineno}: not a valid ISO date: {line!r}") from exc
    return frozenset(dates)


def _parse_dates(d: dict[str, Any], key: str) -> frozenset[date]:
    """Parse a required list of ISO date strings into a frozenset of dates.

    An empty list is valid (no holidays configured yet); a missing key is not —
    a silently-absent holiday list is exactly the kind of unexamined default that
    would run a session on a market holiday.
    """
    value = _require(d, key, list, section="market")
    return frozenset(_parse_date(v, f"market.{key}[]") for v in value)
