"""Session-open login resilience (in-app bounded retry) and the abort paths.

The always-on process no longer exits on a failed login — the retry moved in-app.
These exercise `_login_with_retry` and the credential-missing path with fake
sessions and a stub audit, so no network, credentials, or market are needed.
"""

from __future__ import annotations

import asyncio

import pytest

from argus import session as session_mod
from argus.session import SessionState, _login_with_retry, run_shadow_session
from support import make_config


class _FakeSession:
    """Minimal stand-in for TastytradeSession: counts opens/closes, fails N times."""

    def __init__(self, fail_times: int = 0) -> None:
        self.opened = 0
        self.closed = 0
        self._fail_times = fail_times

    async def open(self):
        self.opened += 1
        if self.opened <= self._fail_times:
            raise RuntimeError("cert down")
        return object()

    async def aclose(self):
        self.closed += 1


class _StubAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []
        self.telem: list[tuple[str, dict]] = []

    async def record(self, category, payload):
        self.records.append((category, payload))

    def telemetry(self, category, payload):
        self.telem.append((category, payload))
        return True


def _cfg(attempts: int = 3):
    return make_config(
        market={"login_attempts": attempts, "login_retry_interval_s": 0.001}
    )


def _categories(items):
    return [c for c, _ in items]


async def test_login_succeeds_first_try():
    tt, md = _FakeSession(), _FakeSession()
    audit = _StubAudit()
    ok = await _login_with_retry(
        tt, md, config=_cfg(), audit=audit, alert=lambda *_: None, stop=asyncio.Event()
    )
    assert ok is True
    assert tt.opened == 1 and md.opened == 1
    assert "session.login_attempt_failed" not in _categories(audit.telem)


async def test_login_recovers_after_transient_failures():
    tt, md = _FakeSession(fail_times=2), _FakeSession()
    audit = _StubAudit()
    ok = await _login_with_retry(
        tt, md, config=_cfg(attempts=5), audit=audit, alert=lambda *_: None, stop=asyncio.Event()
    )
    assert ok is True
    # Two failed attempts (each closes whatever opened), then success on the third.
    assert _categories(audit.telem).count("session.login_attempt_failed") == 2
    assert "session.login_recovered" in _categories(audit.telem)
    assert tt.closed >= 2  # partial opens were cleaned up, not leaked


async def test_login_gives_up_after_the_configured_attempts():
    tt, md = _FakeSession(fail_times=99), _FakeSession()
    audit = _StubAudit()
    alerts: list = []
    ok = await _login_with_retry(
        tt, md, config=_cfg(attempts=3), audit=audit, alert=lambda k, d: alerts.append(k),
        stop=asyncio.Event(),
    )
    assert ok is False
    assert tt.opened == 3  # exactly the configured cap — no tighter, no looser
    assert "session.login_gave_up" in _categories(audit.records)  # Tier 1, durable
    assert "login_gave_up" in alerts


async def test_login_returns_immediately_when_already_stopped():
    tt, md = _FakeSession(), _FakeSession()
    stop = asyncio.Event()
    stop.set()  # SIGTERM before the first attempt
    ok = await _login_with_retry(
        tt, md, config=_cfg(), audit=_StubAudit(), alert=lambda *_: None, stop=stop
    )
    assert ok is False
    assert tt.opened == 0 and md.opened == 0


async def test_missing_credentials_aborts_the_day_without_raising(monkeypatch):
    def _boom(*_a, **_k):
        raise session_mod.MissingCredentialsError(
            "missing TASTYTRADE_TRADING_CERT_CLIENT_SECRET"
        )

    # Patched at the SELECTOR, not at `from_env`: `trading_credentials` is what the
    # session body calls, and it is also the function that raises this on a live
    # profile with no live slot provisioned.
    monkeypatch.setattr(session_mod, "trading_credentials", _boom)
    audit = _StubAudit()
    alerts: list = []

    # Returns cleanly (no session today), records why. state is untouched on this
    # path, so a placeholder is fine.
    await run_shadow_session(
        config=make_config(),
        audit=audit,
        state=object(),  # type: ignore[arg-type]
        stop=asyncio.Event(),
        trading_date=session_mod.date(2026, 7, 27),
        alert=lambda k, d: alerts.append(k),
    )
    assert "session.login_gave_up" in _categories(audit.records)
    assert "login_missing_credentials" in alerts
