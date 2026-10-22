"""TastytradeSession: sandbox-first wiring and credential hygiene."""

from __future__ import annotations

import copy

import pytest

from argus.broker.session import (
    MARKETDATA_LIVE_REFRESH_VAR,
    MARKETDATA_LIVE_SECRET_VAR,
    TRADING_CERT_REFRESH_VAR,
    TRADING_CERT_SECRET_VAR,
    TRADING_LIVE_REFRESH_VAR,
    TRADING_LIVE_SECRET_VAR,
    MissingCredentialsError,
    TastytradeCredentials,
    TastytradeSession,
)
from argus.config import Config
from support import make_config



def _config(environment: str) -> Config:
    overrides: dict = {"environment": environment}
    if environment == "live":
        # config.py refuses a live environment with a non-durable synchronous and
        # without a named account — satisfy both, so a test's OWN subject is the
        # only thing that can fail it.
        overrides["audit"] = {"synchronous": "FULL"}
        overrides["broker"] = {"account_number": "5WX00000"}
    return make_config(**overrides)


class _FakeSession:
    # `**client_kwargs` mirrors the real SDK Session, which forwards extras
    # (notably `timeout`) into its httpx client.
    def __init__(self, provider_secret, refresh_token, is_test, **client_kwargs):
        self.provider_secret = provider_secret
        self.refresh_token = refresh_token
        self.is_test = is_test
        self.client_kwargs = client_kwargs
        self.validated = False
        self.refreshed_force = None
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False

    async def validate(self):
        self.validated = True
        return True

    async def refresh(self, force=False):
        self.refreshed_force = force


_CREDS = TastytradeCredentials(
    provider_secret="sekret",
    refresh_token="reftoken",
    secret_var=TRADING_CERT_SECRET_VAR,
    refresh_var=TRADING_CERT_REFRESH_VAR,
)


@pytest.mark.parametrize(
    "environment,expected_is_test",
    [("shadow", True), ("sandbox", True), ("live", False)],
)
def test_is_test_derived_from_environment(environment, expected_is_test):
    tt = TastytradeSession(_config(environment), _CREDS, session_factory=_FakeSession)
    assert tt.is_test is expected_is_test


def test_connect_passes_credentials_and_test_flag():
    tt = TastytradeSession(_config("sandbox"), _CREDS, session_factory=_FakeSession)
    session = tt.connect()
    assert session.provider_secret == "sekret"
    assert session.refresh_token == "reftoken"
    assert session.is_test is True
    assert tt.session is session


def test_session_before_connect_raises():
    tt = TastytradeSession(_config("shadow"), _CREDS, session_factory=_FakeSession)
    with pytest.raises(RuntimeError, match="connect"):
        _ = tt.session


def test_from_env_reads_credentials(monkeypatch):
    monkeypatch.setenv(TRADING_CERT_SECRET_VAR, "s")
    monkeypatch.setenv(TRADING_CERT_REFRESH_VAR, "r")
    creds = TastytradeCredentials.from_env(TRADING_CERT_SECRET_VAR, TRADING_CERT_REFRESH_VAR)
    assert creds.provider_secret == "s" and creds.refresh_token == "r"
    # It also carries WHICH slot it came from — the /status row and the startup
    # line both read this, and neither may read a value.
    assert creds.slot == "TASTYTRADE_TRADING_CERT_*"


def test_from_env_has_no_defaults():
    """A bare `from_env()` was the trading path until 2026-09-09 and is now the BUG.

    Removing the defaults is what makes that a TypeError at the call site rather
    than cert credentials silently used under a live profile. The AST sweep in
    `test_no_module_builds_credentials_without_naming_the_slot` covers `src/`;
    this covers the constructor itself.
    """
    with pytest.raises(TypeError):
        TastytradeCredentials.from_env()  # type: ignore[call-arg]


def test_from_env_missing_raises_naming_vars_not_values(monkeypatch):
    monkeypatch.delenv(TRADING_CERT_SECRET_VAR, raising=False)
    monkeypatch.setenv(TRADING_CERT_REFRESH_VAR, "r")
    with pytest.raises(MissingCredentialsError) as exc:
        TastytradeCredentials.from_env(TRADING_CERT_SECRET_VAR, TRADING_CERT_REFRESH_VAR)
    assert TRADING_CERT_SECRET_VAR in str(exc.value)
    assert "r" != str(exc.value)  # the present value is never echoed


def test_credentials_repr_hides_secrets():
    r = repr(_CREDS)
    assert "sekret" not in r and "reftoken" not in r
    assert "***" in r


async def test_validate_and_refresh_are_awaited():
    tt = TastytradeSession(_config("sandbox"), _CREDS, session_factory=_FakeSession)
    tt.connect()
    assert await tt.validate() is True
    await tt.refresh(force=True)
    assert tt.session.validated is True
    assert tt.session.refreshed_force is True


async def test_open_enters_context_and_forces_initial_token():
    # open() must fetch the initial token (the SDK does not authenticate on
    # construction) and enter the client context for later clean shutdown.
    tt = TastytradeSession(_config("sandbox"), _CREDS, session_factory=_FakeSession)
    session = await tt.open()
    assert session.entered is True
    assert session.refreshed_force is True  # forced initial refresh
    await tt.aclose()
    assert session.exited is True


# -- session role split: live read-only market data ----------------------


def _md_session(source: str, environment: str = "shadow"):
    from argus.broker.session import SessionRole

    cfg = make_config(environment=environment, marketdata={"source": source})
    return TastytradeSession(cfg, _CREDS, role=SessionRole.MARKET_DATA,
                             session_factory=_FakeSession)


@pytest.mark.parametrize(
    "source,expected_is_test", [("sandbox", True), ("live", False)]
)
def test_market_data_session_follows_marketdata_source(source, expected_is_test):
    # Market data can be LIVE while trading stays cert.
    assert _md_session(source).is_test is expected_is_test


def test_trading_session_ignores_marketdata_source():
    # Live market data must NOT drag the trading session out of sandbox.
    cfg = make_config(environment="shadow", marketdata={"source": "live"})
    tt = TastytradeSession(cfg, _CREDS, session_factory=_FakeSession)
    assert tt.is_test is True  # still cert


def test_market_data_session_refuses_to_be_used_for_trading():
    # The core safety property: nothing that can move money may hold the
    # (possibly live) market-data session.
    md = _md_session("live")
    md.connect()
    with pytest.raises(RuntimeError, match="read-only by design"):
        _ = md.trading_session


def test_trading_session_refuses_market_data_accessor():
    tt = TastytradeSession(make_config(), _CREDS, session_factory=_FakeSession)
    tt.connect()
    with pytest.raises(RuntimeError, match="market-data session"):
        _ = tt.market_data_session


def _provision(monkeypatch, **slots: str) -> None:
    """Set every credential slot, then override the named ones.

    Written as clear-then-set so a developer's real `.env` cannot reach any test
    below — the same discipline `test_app.py` applies before a startup test.
    """
    for var in (
        TRADING_CERT_SECRET_VAR,
        TRADING_CERT_REFRESH_VAR,
        TRADING_LIVE_SECRET_VAR,
        TRADING_LIVE_REFRESH_VAR,
        MARKETDATA_LIVE_SECRET_VAR,
        MARKETDATA_LIVE_REFRESH_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    for var, value in slots.items():
        monkeypatch.setenv(var, value)


_ALL_SLOTS = {
    TRADING_CERT_SECRET_VAR: "cert-secret",
    TRADING_CERT_REFRESH_VAR: "cert-refresh",
    TRADING_LIVE_SECRET_VAR: "live-trading-secret",
    TRADING_LIVE_REFRESH_VAR: "live-trading-refresh",
    MARKETDATA_LIVE_SECRET_VAR: "live-md-secret",
    MARKETDATA_LIVE_REFRESH_VAR: "live-md-refresh",
}


def test_market_data_credentials_use_live_vars_when_live(monkeypatch):
    from argus.broker.session import market_data_credentials

    _provision(monkeypatch, **_ALL_SLOTS)

    live = market_data_credentials(make_config(marketdata={"source": "live"}))
    sandbox = market_data_credentials(make_config(marketdata={"source": "sandbox"}))
    # A cert refresh token cannot read live quotes, so live needs its own.
    assert live.provider_secret == "live-md-secret"
    # Sandbox market data falls back to the cert TRADING slot — the same
    # registration, now named as such.
    assert sandbox.provider_secret == "cert-secret"
    assert sandbox.slot == "TASTYTRADE_TRADING_CERT_*"


def test_live_market_data_requires_live_credentials(monkeypatch):
    from argus.broker.session import market_data_credentials

    _provision(monkeypatch, **{MARKETDATA_LIVE_REFRESH_VAR: "r"})
    with pytest.raises(MissingCredentialsError, match=MARKETDATA_LIVE_SECRET_VAR):
        market_data_credentials(make_config(marketdata={"source": "live"}))


# -- trading credentials are keyed on `environment` (2026-09-09) --------------


@pytest.mark.parametrize(
    "environment,expected_secret,expected_slot",
    [
        ("shadow", "cert-secret", "TASTYTRADE_TRADING_CERT_*"),
        ("sandbox", "cert-secret", "TASTYTRADE_TRADING_CERT_*"),
        ("live", "live-trading-secret", "TASTYTRADE_TRADING_LIVE_*"),
    ],
)
def test_trading_credentials_follow_environment(
    monkeypatch, environment, expected_secret, expected_slot
):
    """The credential is keyed on the same authority `is_test` keys the ENDPOINT on.

    This is the whole change: before it, all three rows returned the cert secret,
    so `live` sent cert values to the production API.
    """
    from argus.broker.session import trading_credentials

    _provision(monkeypatch, **_ALL_SLOTS)
    creds = trading_credentials(_config(environment))
    assert creds.provider_secret == expected_secret
    assert creds.slot == expected_slot


def test_live_trading_refuses_when_only_cert_credentials_are_present(monkeypatch):
    """The live refusal, and it is free — `from_env` raises naming the two vars.

    Analogous to config.py's `account_stream_max_age_s` / `audit.synchronous` live
    refusals, but it lives in the broker layer: the config loader has no business
    reading credentials.
    """
    from argus.broker.session import trading_credentials

    _provision(
        monkeypatch,
        **{
            TRADING_CERT_SECRET_VAR: "cert-secret",
            TRADING_CERT_REFRESH_VAR: "cert-refresh",
        },
    )
    with pytest.raises(MissingCredentialsError) as exc:
        trading_credentials(_config("live"))
    assert TRADING_LIVE_SECRET_VAR in str(exc.value)
    assert TRADING_LIVE_REFRESH_VAR in str(exc.value)


def test_a_cert_profile_needs_no_live_credentials(monkeypatch):
    """The refusal is one-way. A dev machine with no live registration still runs."""
    from argus.broker.session import trading_credentials

    _provision(
        monkeypatch,
        **{
            TRADING_CERT_SECRET_VAR: "cert-secret",
            TRADING_CERT_REFRESH_VAR: "cert-refresh",
        },
    )
    assert trading_credentials(_config("sandbox")).provider_secret == "cert-secret"


@pytest.mark.parametrize(
    "duplicated", [TRADING_LIVE_SECRET_VAR, TRADING_LIVE_REFRESH_VAR]
)
def test_live_slot_carrying_cert_values_is_refused(monkeypatch, duplicated):
    """Guarantee (ii): the migration itself manufactures this state.

    Moving to the (role, world) names copies existing values into the CERT slots.
    An operator who copies into the LIVE slots too gets well-formed variables
    carrying cert values — and then six `invalid_grant` logins ten minutes apart,
    each accruing toward tastytrade's IP-block threshold.
    """
    from argus.broker.session import trading_credentials

    slots = dict(_ALL_SLOTS)
    slots[duplicated] = (
        "cert-secret" if duplicated == TRADING_LIVE_SECRET_VAR else "cert-refresh"
    )
    _provision(monkeypatch, **slots)

    with pytest.raises(MissingCredentialsError) as exc:
        trading_credentials(_config("live"))
    assert duplicated in str(exc.value)
    # Names, never values.
    assert "cert-secret" not in str(exc.value)
    assert "cert-refresh" not in str(exc.value)


def test_the_identical_check_does_not_fire_in_cert(monkeypatch):
    """It is scoped to `live`. Cert reads the cert slot and compares nothing."""
    from argus.broker.session import trading_credentials

    slots = dict(_ALL_SLOTS)
    slots[TRADING_LIVE_SECRET_VAR] = "cert-secret"
    _provision(monkeypatch, **slots)
    assert trading_credentials(_config("sandbox")).provider_secret == "cert-secret"


def test_credential_slot_labels_read_no_environment(monkeypatch):
    """The `/status` scalars are a pure function of config — no env, no values."""
    from argus.broker.session import credential_slot_labels

    _provision(monkeypatch)  # nothing set at all
    live = make_config(
        environment="live",
        audit={"synchronous": "FULL"},
        broker={"account_number": "5WX00000"},
        marketdata={"source": "live"},
    )
    assert credential_slot_labels(live) == (
        "TASTYTRADE_TRADING_LIVE_*",
        "TASTYTRADE_MARKETDATA_LIVE_*",
    )
    # The two axes are INDEPENDENT — this is the shipped cert shape, live quotes
    # against cert orders, and it must not report the live trading slot.
    assert credential_slot_labels(make_config(marketdata={"source": "live"})) == (
        "TASTYTRADE_TRADING_CERT_*",
        "TASTYTRADE_MARKETDATA_LIVE_*",
    )
    assert credential_slot_labels(make_config(marketdata={"source": "sandbox"})) == (
        "TASTYTRADE_TRADING_CERT_*",
        "TASTYTRADE_TRADING_CERT_*",
    )


async def test_open_cleans_up_on_login_failure():
    class _FailingSession(_FakeSession):
        async def refresh(self, force=False):
            raise RuntimeError("bad creds")

    tt = TastytradeSession(_config("sandbox"), _CREDS, session_factory=_FailingSession)
    with pytest.raises(RuntimeError, match="bad creds"):
        await tt.open()
    # The client context must be exited even though login failed.
    with pytest.raises(RuntimeError, match="connect"):
        _ = tt.session


# -- the HTTP timeout must actually reach the SDK client ------------------


def test_connect_passes_the_configured_timeout_to_the_sdk():
    """Regression: the SDK builds its httpx client with no timeout, so without
    this the client silently used httpx's 5s default — which killed the
    droplet's first shadow run on a cold /customers/me/accounts."""
    cfg = make_config(broker={"http_timeout_s": 42.0})
    tt = TastytradeSession(cfg, _CREDS, session_factory=_FakeSession)
    assert tt.connect().client_kwargs["timeout"] == 42.0


def test_market_data_session_also_gets_the_timeout():
    from argus.broker.session import SessionRole

    cfg = make_config(broker={"http_timeout_s": 12.5})
    md = TastytradeSession(cfg, _CREDS, role=SessionRole.MARKET_DATA,
                           session_factory=_FakeSession)
    assert md.connect().client_kwargs["timeout"] == 12.5


# -- the connection pool must outlive the liveness refresh ----------------


def test_keepalive_expiry_exceeds_the_account_refresh_interval():
    """Regression: httpx's default keepalive_expiry (5s) was BELOW
    `account_refresh_s` (10s), so every liveness refresh re-handshaked — two
    fresh TLS connections every 10s, ~4,500 a session, on the event loop.

    Asserted as a RELATION, not a literal: the defect was the two values
    crossing, and only a relation catches them crossing again.
    """
    cfg = make_config(staleness={"account_refresh_s": 9.0,
                                 "account_stream_max_age_s": 20.0,
                                 "account_refresh_timeout_s": 3.0})
    tt = TastytradeSession(cfg, _CREDS, session_factory=_FakeSession)
    limits = tt.connect().client_kwargs["limits"]
    assert limits.keepalive_expiry > cfg.staleness.account_refresh_s
    assert limits.keepalive_expiry == 27.0  # 9.0 * KEEPALIVE_REFRESH_MULTIPLE


def test_keepalive_tracks_a_retuned_refresh_interval():
    """It is DERIVED, not a constant — retuning the refresh must carry it along."""
    fast = make_config(staleness={"account_refresh_s": 2.0,
                                  "account_stream_max_age_s": 5.0,
                                  "account_refresh_timeout_s": 1.0})
    slow = make_config(staleness={"account_refresh_s": 20.0,
                                  "account_stream_max_age_s": 45.0,
                                  "account_refresh_timeout_s": 5.0,
                                  # Moves with the refresh interval, which is the
                                  # relation the loader enforces.
                                  "account_position_evict_after_s": 120.0})
    for cfg in (fast, slow):
        limits = TastytradeSession(cfg, _CREDS,
                                   session_factory=_FakeSession).connect(
                                   ).client_kwargs["limits"]
        assert limits.keepalive_expiry > cfg.staleness.account_refresh_s


def test_pool_size_limits_are_stated_not_left_unlimited():
    """`Limits(keepalive_expiry=x)` leaves the other two None — which means
    UNLIMITED, not 'keep httpx's default'. Passing `limits` at all replaces the
    whole object, so the pool bounds have to be restated or this fix silently
    removes them."""
    limits = TastytradeSession(make_config(), _CREDS,
                               session_factory=_FakeSession).connect(
                               ).client_kwargs["limits"]
    assert limits.max_connections == 100
    assert limits.max_keepalive_connections == 20


def test_market_data_session_also_gets_the_pool_limits():
    from argus.broker.session import SessionRole

    md = TastytradeSession(make_config(), _CREDS, role=SessionRole.MARKET_DATA,
                           session_factory=_FakeSession)
    assert md.connect().client_kwargs["limits"].max_connections == 100
