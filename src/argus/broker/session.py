"""Tastytrade session wrapper (spec §5 — reuse the OAuth pattern, own credentials).

Two responsibilities, both safety-relevant:

1. **Credentials never touch the code or logs.** They come from the environment —
   keep them in `.env`, which is gitignored. `from_env` raises if any is missing,
   naming only the VARIABLES, and never echoes a value. These are Argus's OWN app
   registrations, never TOMIC's (CLAUDE.md non-negotiable) — a bug or revocation
   here must not touch TOMIC's session.

2. **Sandbox-first is enforced at the boundary.** `is_test` is derived from
   `config.environment`: only `environment == "live"` hits the real tastytrade
   API; `shadow` and `sandbox` both use the cert/sandbox environment. You cannot
   accidentally point shadow/sandbox code at the live endpoint — it's not a
   convention, it's the wiring.

3. **The CREDENTIAL is keyed on the same authority as the ENDPOINT** (2026-09-09).
   Until then `is_test` was config-derived and the trading credential was not: every
   trading call site read one unprefixed pair, so going live sent CERT values to the
   production API and the documented one-file cutover was secretly also a hand-swap
   of `.env`. Both axes now come from `config`, through `trading_credentials` and
   `market_data_credentials` — the only two ways to build a credential in `src/`,
   held by an AST sweep rather than by convention.
   -> `docs/decisions/2026-09-09-trading-credentials-are-environment-keyed.md`

The actual network connect is delegated to the tastytrade SDK; `session_factory`
is injectable so the wiring logic is unit-testable without credentials.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from httpx2 import Limits
from tastytrade import Session

from argus.config import Config

SessionFactory = Callable[..., Session]

#: How much longer than the fastest recurring caller a pooled connection is kept.
#:
#: `keepalive_expiry` must comfortably EXCEED the account liveness refresh
#: interval or every refresh re-handshakes (see `connect`). 3x leaves room for
#: the refresh's own 1s/2s/4s failure backoff to stretch the gap between two
#: successful calls without dropping the connection underneath it.
#:
#: A module constant rather than a config key, for the reason `notify.py`'s
#: `DEFAULT_THROTTLE_S` is one: it tunes transport and nothing about trading, so
#: it stays out of the fail-closed loader, and a new REQUIRED key would couple a
#: performance fix to a config deploy. It needs no upper bound of its own —
#: `account_refresh_s` is already validated below `account_stream_max_age_s`.
KEEPALIVE_REFRESH_MULTIPLE = 3.0

#: httpx's OWN defaults, restated because passing `limits` at all replaces the
#: whole `Limits` object: `Limits(keepalive_expiry=x)` leaves the other two None,
#: which means UNLIMITED, not "keep the default". Same family of trap as the
#: silent 5s timeout below — an override that quietly widens something else.
POOL_MAX_CONNECTIONS = 100
POOL_MAX_KEEPALIVE_CONNECTIONS = 20


class SessionRole(StrEnum):
    """What a session is allowed to be used for.

    TRADING sessions reach the account and (later) route orders; their
    environment follows `config.environment`, so sandbox-first still holds.

    MARKET_DATA sessions are READ-ONLY by design — quotes, greeks, instruments.
    They may point at the live API (`config.marketdata.source = "live"`) so shadow
    mode can measure signal quality against real-time prices instead of 15-minute
    delayed ones. Handing one to account or order code raises: the split is
    enforced structurally, not by convention, because "read-only" that depends on
    everyone remembering is not read-only.
    """

    TRADING = "trading"
    MARKET_DATA = "market_data"

# Matches the names in the project .env. Only secret + refresh are consumed:
# tastytrade's refresh-token grant uses client_secret + refresh_token. The client ID
# is used only to originally mint the refresh token and is not needed here.
#
# TWO AXES, and every slot names BOTH: `TASTYTRADE_<ROLE>_<WORLD>_<FIELD>`.
#
#   ROLE   TRADING can move money; MARKETDATA is read-only quotes/greeks/instruments.
#   WORLD  CERT is the sandbox API; LIVE is production. Separate logins entirely —
#          a cert refresh token cannot read live quotes and a live one cannot
#          authenticate against cert.
#
# ROLE FIRST, deliberately: role is the safety-relevant axis, so
# `grep TASTYTRADE_TRADING_` is a complete audit of every credential that can reach
# an order. World-first (`TASTYTRADE_LIVE_TRADING_*`) groups by blast radius, which
# is the less useful grouping, and it preserves a `TASTYTRADE_LIVE_` prefix meaning
# two different things depending on what follows — which is the exact defect
# 2026-09-09 removed. The pre-2026-09-09 names (`TASTYTRADE_CLIENT_SECRET`,
# `TASTYTRADE_REFRESH_TOKEN`, `TASTYTRADE_LIVE_*`) are GONE, not aliased: an alias
# is a second name for one credential, which is the disease. They were also
# byte-identical to TOMIC's, isolated only by living in separate `.env` files.
TRADING_CERT_SECRET_VAR = "TASTYTRADE_TRADING_CERT_CLIENT_SECRET"
TRADING_CERT_REFRESH_VAR = "TASTYTRADE_TRADING_CERT_REFRESH_TOKEN"
TRADING_LIVE_SECRET_VAR = "TASTYTRADE_TRADING_LIVE_CLIENT_SECRET"
TRADING_LIVE_REFRESH_VAR = "TASTYTRADE_TRADING_LIVE_REFRESH_TOKEN"
MARKETDATA_LIVE_SECRET_VAR = "TASTYTRADE_MARKETDATA_LIVE_CLIENT_SECRET"
MARKETDATA_LIVE_REFRESH_VAR = "TASTYTRADE_MARKETDATA_LIVE_REFRESH_TOKEN"


class MissingCredentialsError(RuntimeError):
    """Raised when required tastytrade credentials are absent from the environment."""


@dataclass(frozen=True)
class TastytradeCredentials:
    provider_secret: str
    refresh_token: str
    #: WHICH SLOT these came from. Variable NAMES are not secret, and carrying them
    #: is what lets the startup line and `/status` say which registration a session
    #: authenticated with — the credential half of the config fingerprint's
    #: question, one layer further out than config and previously unobservable.
    secret_var: str
    refresh_var: str

    @staticmethod
    def from_env(secret_var: str, refresh_var: str) -> "TastytradeCredentials":
        """Read one (role, world) slot from the environment.

        **No defaults, deliberately.** Until 2026-09-09 this defaulted to the
        unprefixed cert pair, which made a bare `from_env()` the trading path — and
        therefore made it the BUG once the trading path became environment-keyed:
        cert credentials under a live profile, silently, at whichever call site was
        missed or added later. Callers name the slot, or go through
        `trading_credentials` / `market_data_credentials`, which name it for them.
        """
        secret = os.environ.get(secret_var)
        refresh = os.environ.get(refresh_var)
        missing = [v for v, val in ((secret_var, secret), (refresh_var, refresh)) if not val]
        if missing:
            # Report only the missing variable NAMES — never any value.
            raise MissingCredentialsError(
                f"missing required tastytrade credential env var(s): {', '.join(missing)}"
            )
        assert secret is not None and refresh is not None
        return TastytradeCredentials(
            provider_secret=secret,
            refresh_token=refresh,
            secret_var=secret_var,
            refresh_var=refresh_var,
        )

    @property
    def slot(self) -> str:
        """Display label for the pair, e.g. `TASTYTRADE_TRADING_CERT_*`. Names only."""
        return f"{self.secret_var.removesuffix('CLIENT_SECRET')}*"

    def __repr__(self) -> str:  # never leak secrets in a repr / traceback
        return (
            f"TastytradeCredentials(slot={self.slot}, "
            "provider_secret=***, refresh_token=***)"
        )


class TastytradeSession:
    def __init__(
        self,
        config: Config,
        credentials: TastytradeCredentials,
        *,
        role: SessionRole = SessionRole.TRADING,
        session_factory: SessionFactory = Session,
    ) -> None:
        self._config = config
        self._credentials = credentials
        self._role = role
        self._factory = session_factory
        self._session: Session | None = None

    @property
    def role(self) -> SessionRole:
        return self._role

    @property
    def is_test(self) -> bool:
        """Which API this session talks to.

        Trading sessions are sandbox/cert unless `environment == "live"` — the
        sandbox-first guarantee. Market-data sessions follow
        `marketdata.source`, so read-only live quotes are possible while orders
        stay in cert.
        """
        if self._role is SessionRole.MARKET_DATA:
            return self._config.marketdata.source != "live"
        return self._config.environment != "live"

    def connect(self) -> Session:
        """Construct the SDK Session object. Does NO network I/O — the object
        starts with a placeholder token; `open()` performs the actual login.

        `timeout` is forwarded into the SDK's httpx client (`client_kwargs`).
        Without it the client silently inherits httpx's 5-second default, which
        is not a number anyone chose and is too tight for the cert API — see
        `BrokerConfig.http_timeout_s`.

        `limits` is the SAME TRAP, one field over, and it was live until
        2026-08-06. httpx's `DEFAULT_LIMITS.keepalive_expiry` is 5.0s while
        `[staleness] account_refresh_s` is 10.0s, so every liveness refresh found
        its pooled connection already expired — and `refresh_now` fires positions
        and balances concurrently, so it opened TWO fresh HTTPS connections and
        paid TWO full TLS handshakes, every 10 seconds. About 4,500 handshakes a
        session, all on the event loop.

        Measured against a local TLS stub over 2,400 refreshes: 4.2ms per refresh
        with a handshake every time versus 1.3ms on a pooled connection — a 3.2x
        multiple, ~3ms of avoidable CPU each time on this Mac and roughly 4x that
        on the droplet's vCPU. Real cost is higher again: a local stub has no
        network RTT and a one-cert chain.

        This is NOT the intra-session lag ramp — the same harness showed per-call
        cost and object count FLAT across a full session's worth of calls, under
        stock CPython GC and under `tune_gc`'s settings alike, which is what ruled
        the REST path out as the accumulator. It is simply the cheapest reduction
        available on the ~10s-period event whose cost sits near the heartbeat's
        50ms breach threshold.

        Derived from `account_refresh_s` rather than hardcoded so the two cannot
        silently cross again if the refresh interval is ever retuned — which is
        exactly how this arose.
        """
        self._session = self._factory(
            provider_secret=self._credentials.provider_secret,
            refresh_token=self._credentials.refresh_token,
            is_test=self.is_test,
            timeout=self._config.broker.http_timeout_s,
            limits=Limits(
                max_connections=POOL_MAX_CONNECTIONS,
                max_keepalive_connections=POOL_MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=(
                    self._config.staleness.account_refresh_s * KEEPALIVE_REFRESH_MULTIPLE
                ),
            ),
        )
        return self._session

    async def open(self) -> Session:
        """Construct, enter the httpx client context, and fetch the initial token.

        The SDK does not authenticate on construction — it only acquires a bearer
        token lazily on its first authenticated request (or via `refresh`). We
        force that refresh here so the session is usable immediately and a bad
        credential fails fast, right at startup, instead of on the first order.
        Entering the client context ensures the httpx connection pool is cleaned
        up by `aclose()` rather than leaked by the always-on process.
        """
        session = self.connect()
        await session.__aenter__()
        try:
            await session.refresh(force=True)
        except BaseException:
            await session.__aexit__(*sys.exc_info())
            self._session = None
            raise
        return session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("connect()/open() must be called before accessing the session")
        return self._session

    @property
    def trading_session(self) -> Session:
        """SDK session for account access and (later) order routing.

        Raises for a market-data session. That session may be pointed at the LIVE
        API for real-time quotes, and nothing that can move money is allowed to
        hold it — enforced here rather than trusted to call sites.
        """
        if self._role is not SessionRole.TRADING:
            raise RuntimeError(
                f"refusing to hand a {self._role} session to account/order code: "
                "the market-data session is read-only by design"
            )
        return self.session

    @property
    def market_data_session(self) -> Session:
        """SDK session for quotes, greeks, and instruments. Read-only usage."""
        if self._role is not SessionRole.MARKET_DATA:
            raise RuntimeError(f"expected a market-data session, got role={self._role}")
        return self.session

    async def validate(self) -> bool:
        # SDK's validate() is a coroutine — must be awaited.
        return await self.session.validate()

    async def refresh(self, force: bool = False) -> None:
        # SDK's refresh() is a coroutine — must be awaited.
        await self.session.refresh(force=force)


def trading_credentials(config: Config) -> TastytradeCredentials:
    """Credentials for the TRADING session — account reads and order routing.

    Keyed on `config.environment`, the same authority `is_test` derives the ENDPOINT
    from, so the credential and the API it is sent to can no longer disagree. Before
    2026-09-09 this was a bare `TastytradeCredentials.from_env()` at all seven call
    sites: going live sent CERT values to the production API, so the cutover was
    secretly a hand-swap of `/opt/argus/.env` on top of the documented `host.env`
    profile flip — a second step that no assertion could see, that the rollback did
    not undo, and that could not be rehearsed without breaking cert operation.

    **The live refusal is free and needs no separate check.** Under
    `environment = "live"` this reads the live trading slot, so a live profile with
    no live registration provisioned raises `MissingCredentialsError` naming exactly
    the two missing variables, at startup, before any network call. That is the
    analogue of `config.py`'s `account_stream_max_age_s` and `audit.synchronous`
    live refusals — but it lives HERE and not there, deliberately: the config loader
    has no business reading credentials, and giving a fail-closed loader a reason to
    touch the environment is the wrong direction. A cert profile needs only the cert
    slot, so a dev machine with no live registration is unaffected.
    """
    if config.environment != "live":
        return TastytradeCredentials.from_env(
            TRADING_CERT_SECRET_VAR, TRADING_CERT_REFRESH_VAR
        )
    creds = TastytradeCredentials.from_env(
        TRADING_LIVE_SECRET_VAR, TRADING_LIVE_REFRESH_VAR
    )
    _refuse_cert_values_in_the_live_slot(creds)
    return creds


def credential_slot_labels(config: Config) -> tuple[str, str]:
    """`(trading, market data)` slot labels this config selects. Names only.

    Reads NO environment and holds no value — it answers "which slot would be
    used?", which is a pure function of `environment` and `marketdata.source`, the
    same two keys the two selectors above branch on. That is what makes it safe for
    `Application.startup` to lift onto `/status` as scalars, beside `environment`
    and the config fingerprint.

    It reports the slot the config SELECTS, not one proved present — but a process
    that is up has already built both sessions through the selectors, so the
    distinction is only reachable before startup finishes.
    """
    trading = (
        TRADING_LIVE_SECRET_VAR
        if config.environment == "live"
        else TRADING_CERT_SECRET_VAR
    )
    market_data = (
        MARKETDATA_LIVE_SECRET_VAR
        if config.marketdata.source == "live"
        else TRADING_CERT_SECRET_VAR
    )
    return (
        f"{trading.removesuffix('CLIENT_SECRET')}*",
        f"{market_data.removesuffix('CLIENT_SECRET')}*",
    )


def _refuse_cert_values_in_the_live_slot(live: TastytradeCredentials) -> None:
    """Refuse a live trading slot that is byte-identical to the cert one.

    **This is not hypothetical — the migration manufactures it.** Moving to the
    (role, world) names is an additive three-step: add the new variables alongside
    the old, copying existing values into the CERT slots. An operator who copies
    into the live slots as well ends up with well-formed live variables carrying
    cert values, and the process then presents a cert token to the production API:
    `invalid_grant`, six times, ten minutes apart, each attempt accruing toward
    tastytrade's undocumented IP-block threshold (`_login_with_retry`).

    `tools.credcheck` has performed exactly this comparison for the market-data pair
    since it was written. This promotes the reasoning from a diagnostic somebody has
    to remember to run into a rail that runs itself, on the one environment where
    being wrong costs real money.

    Compares values, reports only NAMES.
    """
    cert_secret = os.environ.get(TRADING_CERT_SECRET_VAR)
    cert_refresh = os.environ.get(TRADING_CERT_REFRESH_VAR)
    same = [
        name
        for name, live_value, cert_value in (
            (TRADING_LIVE_SECRET_VAR, live.provider_secret, cert_secret),
            (TRADING_LIVE_REFRESH_VAR, live.refresh_token, cert_refresh),
        )
        if cert_value and live_value == cert_value
    ]
    if same:
        raise MissingCredentialsError(
            f"environment='live' but {', '.join(same)} holds the same value as the "
            f"corresponding {TRADING_CERT_SECRET_VAR.removesuffix('CLIENT_SECRET')}* "
            f"variable: a cert credential cannot authenticate against the production "
            f"API, and six login attempts ten minutes apart accrue toward "
            f"tastytrade's IP-block threshold. Paste the LIVE registration's own "
            f"values (see `python -m argus.tools.credcheck`)"
        )


def market_data_credentials(config: Config) -> TastytradeCredentials:
    """Credentials for the market-data session.

    Live market data requires a SEPARATE live registration — a cert refresh token
    cannot read production quotes. Falls back to the cert TRADING slot when reading
    sandbox data: it is the same registration, and that fallback is the path
    `argus.dev.toml` and any future offline profile take. Unreachable in both
    shipped profiles today, which set `source = "live"`.
    """
    if config.marketdata.source == "live":
        return TastytradeCredentials.from_env(
            MARKETDATA_LIVE_SECRET_VAR, MARKETDATA_LIVE_REFRESH_VAR
        )
    return TastytradeCredentials.from_env(
        TRADING_CERT_SECRET_VAR, TRADING_CERT_REFRESH_VAR
    )
