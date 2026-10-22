"""Account liveness floor: a quiet account must not be misread as a dead link.

The AlertStreamer pushes only on change, so a flat account emits nothing and
time-since-last-event grows forever. Without a periodic REST re-seed the
staleness gate blocks every entry — worst exactly when flat, i.e. when the first
entry of the day would be placed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx2
import pytest

from argus.account.fake import FakeBrokerAccount
from argus.account.models import Balances, Position
from argus.account.stream import AlertStreamerAccount, is_auth_error


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def _account(clock, refresher, **kw):
    # session/account are unused by the refresh path; start() is never called.
    return AlertStreamerAccount(object(), object(), clock=clock, refresher=refresher, **kw)


async def test_quiet_account_would_go_stale_without_refresh():
    # Reproduces the bug the shadow run surfaced.
    clock = FakeClock(0.0)
    broker = FakeBrokerAccount([], net_liquidating_value=100_000.0)
    acct = _account(clock, None)  # no refresher
    acct.seed(await broker.fetch_positions(), await broker.fetch_balances())
    clock.t = 20.0
    assert acct.age() == 20.0
    assert acct.is_stale(5.0) is True  # link is fine, but the gate would block


async def test_refresh_restores_freshness():
    clock = FakeClock(0.0)
    broker = FakeBrokerAccount([], net_liquidating_value=100_000.0)
    acct = _account(clock, broker)
    acct.seed(await broker.fetch_positions(), await broker.fetch_balances())
    clock.t = 20.0
    assert acct.is_stale(5.0) is True

    assert await acct.refresh_now() is True
    assert acct.age() == 0.0
    assert acct.is_stale(5.0) is False
    # Data is still correct after the re-seed.
    assert acct.snapshot().net_liquidating_value == 100_000.0


class _Failing:
    """A refresher that always fails. One method — the port is one method."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc or RuntimeError("link down")

    async def fetch_snapshot(self):
        raise self.exc


async def test_failed_refresh_leaves_staleness_growing():
    clock = FakeClock(0.0)
    seed_broker = FakeBrokerAccount([], net_liquidating_value=100_000.0)
    acct = _account(clock, _Failing())
    acct.seed(await seed_broker.fetch_positions(), await seed_broker.fetch_balances())
    clock.t = 20.0

    assert await acct.refresh_now() is False
    # Crucially NOT reset: a genuinely broken link must still look broken so the
    # gate keeps failing closed.
    assert acct.age() == 20.0
    assert acct.is_stale(5.0) is True


async def test_refresh_without_refresher_is_a_noop():
    acct = _account(FakeClock(), None)
    assert await acct.refresh_now() is False


# -- ghost positions: a missed close frame must not survive the re-seed -------
#
# 2026-08-19. A close event lost during a stream outage left an entry that NOTHING
# could clear: the REST snapshot OMITS a closed position rather than reporting it
# flat, and `seed` merged the snapshot in without subtracting. Live effect — the
# cache read 3 legs against REST's 2 for over an hour, pinning
# `open_spread_equivalent` at the concurrent limit and paging the one watchdog
# condition with no settle allowance.
# → docs/decisions/2026-08-19-account-cache-ghost-positions.md


def _pos(symbol: str, qty: int) -> Position:
    return Position(
        symbol=symbol,
        underlying="SPXW",
        instrument_type="Equity Option",
        signed_quantity=qty,
        average_open_price=3.0,
        mark_price=3.0,
    )


async def test_seed_evicts_a_position_rest_no_longer_reports():
    clock = FakeClock(0.0)
    held, ghost = _pos("SPXW  260819P07685000", -2), _pos("SPXW  260819P07690000", -2)
    broker = FakeBrokerAccount([held, ghost])
    acct = _account(clock, broker, position_evict_after_s=60.0)
    acct.seed(await broker.fetch_positions(), await broker.fetch_balances())
    assert acct.snapshot().open_position_count == 2

    # The ghost closes; its zero-quantity frame is LOST (stream outage). REST stops
    # reporting it — by omission, which is the only signal there is.
    broker.set_positions([held])

    # Not yet: inside the window it survives, because this is indistinguishable
    # from a fill the broker's book has not caught up on.
    clock.t = 30.0
    assert await acct.refresh_now() is True
    assert acct.snapshot().open_position_count == 2

    # Past the window, with the real leg re-touched by every seed and the ghost
    # touched by none, they separate and it goes.
    clock.t = 91.0
    assert await acct.refresh_now() is True
    snap = acct.snapshot()
    assert snap.open_position_count == 1
    assert [p.symbol for p in snap.positions] == [held.symbol]


async def test_seed_never_evicts_a_position_inside_the_propagation_window():
    """The direction that fails OPEN, and the reason eviction is age-gated.

    A fill books into the cache from the stream immediately; the broker's own
    position book trails it by ~4-5s. Evicting on absence alone would drop a real,
    just-opened leg and release a concurrent slot that is already spoken for.
    """
    clock = FakeClock(0.0)
    broker = FakeBrokerAccount([])
    acct = _account(clock, broker, position_evict_after_s=60.0)
    acct.seed(await broker.fetch_positions(), await broker.fetch_balances())

    # Stream fill lands, through the real wire->domain path. REST still shows
    # nothing — the broker's book has not caught up.
    acct._ingest_position(
        SimpleNamespace(
            symbol="SPXW  260819P07685000",
            underlying_symbol="SPXW",
            instrument_type="Equity Option",
            quantity=2,
            quantity_direction="Short",
            average_open_price=3.0,
            mark_price=3.0,
        )
    )
    assert acct.snapshot().open_position_count == 1

    clock.t = 5.0
    assert await acct.refresh_now() is True
    assert acct.snapshot().open_position_count == 1, "evicted a real just-filled leg"


async def test_eviction_is_off_when_no_threshold_is_configured():
    clock = FakeClock(0.0)
    ghost = _pos("SPXW  260819P07690000", -2)
    broker = FakeBrokerAccount([ghost])
    acct = _account(clock, broker)  # no position_evict_after_s
    acct.seed(await broker.fetch_positions(), await broker.fetch_balances())
    broker.set_positions([])

    clock.t = 10_000.0
    assert await acct.refresh_now() is True
    assert acct.snapshot().open_position_count == 1


async def test_eviction_records_telemetry_with_the_age_it_reached():
    records: list[tuple[str, dict]] = []
    clock = FakeClock(0.0)
    ghost = _pos("SPXW  260819P07690000", -2)
    broker = FakeBrokerAccount([ghost])
    acct = _account(
        clock, broker, position_evict_after_s=60.0, telemetry=lambda c, p: records.append((c, p))
    )
    acct.seed(await broker.fetch_positions(), await broker.fetch_balances())
    broker.set_positions([])

    clock.t = 75.0
    assert await acct.refresh_now() is True
    evicted = [p for c, p in records if c == "account.position_evicted"]
    assert len(evicted) == 1
    assert evicted[0]["symbol"] == ghost.symbol
    assert evicted[0]["signed_quantity"] == -2
    # The measurement the threshold gets re-tuned from.
    assert evicted[0]["age_s"] == 75.0
    assert evicted[0]["evict_after_s"] == 60.0


async def test_failed_refresh_never_evicts():
    """A partial or failed fetch is not evidence of absence."""
    clock = FakeClock(0.0)
    held = _pos("SPXW  260819P07685000", -2)
    seed_broker = FakeBrokerAccount([held])
    acct = _account(clock, _Failing(), position_evict_after_s=60.0)
    acct.seed(await seed_broker.fetch_positions(), await seed_broker.fetch_balances())

    clock.t = 10_000.0
    assert await acct.refresh_now() is False
    assert acct.snapshot().open_position_count == 1


async def test_refresh_loop_runs_periodically():
    calls: list[int] = []

    class _Counting:
        async def fetch_snapshot(self):
            calls.append(1)
            return [], Balances(net_liquidating_value=100_000.0)

    acct = _account(FakeClock(), _Counting(), refresh_interval_s=0.01)
    task = asyncio.create_task(acct._refresh_loop())
    await asyncio.sleep(0.06)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(calls) >= 2  # refreshed repeatedly, not just once


# -- hardening: the cert API returns 502s, hangs, and expires tokens ---------
#
# All four behaviours below were observed in a single 90-second window of a live
# shadow run on 2026-07-21, during which 12 candidates were denied.


class _Boom:
    """A refresher that fails a fixed number of times, then succeeds."""

    def __init__(self, exc, fail_times=10**9):
        self.exc = exc
        self.fail_times = fail_times
        self.attempts = 0

    async def fetch_snapshot(self):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.exc
        return [], Balances(net_liquidating_value=100_000.0)


async def test_a_port_that_bounds_itself_is_simply_reported():
    """The normal timeout path after 2026-08-26: the PORT raises, we record.

    `refresh_now` no longer wraps the call — see the docstring there. The port's
    own budget timeout is a builtin `TimeoutError` precisely so `error_type`
    keeps reading "TimeoutError" for every audit query written before this change.
    """
    events = []
    acct = _account(
        FakeClock(), _Boom(TimeoutError("positions: ReadTimeout: timed out")),
        refresh_interval_s=10.0, telemetry=lambda c, p: events.append((c, p)),
    )
    assert await acct.refresh_now() is False
    assert acct.consecutive_refresh_failures == 1
    assert events[0][1]["error_type"] == "TimeoutError"
    assert events[0][1]["error_class"] == "timeout"


async def test_a_port_that_does_not_bound_itself_is_caught_by_the_backstop():
    """The contract is enforced by a test and a backstop, not by the type system.

    The backstop must never fire in production; when it does, an implementation
    broke its contract, and it says so in its own exception type rather than
    hiding inside the ordinary `TimeoutError` volume.
    """

    class _Hangs:
        async def fetch_snapshot(self):
            await asyncio.sleep(60)

    events = []
    acct = _account(FakeClock(), _Hangs(), refresh_interval_s=0.02,
                    telemetry=lambda c, p: events.append((c, p)))
    assert await asyncio.wait_for(acct.refresh_now(), timeout=1.0) is False
    assert acct.consecutive_refresh_failures == 1
    payload = events[0][1]
    assert payload["error_type"] == "RefreshBackstopError"
    assert payload["error_class"] == "backstop"
    # A contract breach is not a credential problem: it must not drive `_try_reauth`.
    assert payload["auth_error"] is False


async def test_the_backstop_is_the_refresh_interval_not_an_arbitrary_multiple():
    """If an attempt has not returned by the time the next one is due, stop waiting."""
    started = asyncio.get_running_loop().time()

    class _Hangs:
        async def fetch_snapshot(self):
            await asyncio.sleep(60)

    acct = _account(FakeClock(), _Hangs(), refresh_interval_s=0.05)
    assert await acct.refresh_now() is False
    assert asyncio.get_running_loop().time() - started == pytest.approx(0.05, abs=0.05)


async def test_backoff_retries_fast_then_widens_to_the_interval():
    acct = _account(FakeClock(), _Boom(RuntimeError("502")), refresh_interval_s=10.0)
    # A transient blip must not cost the full interval: with a 10s interval
    # against a 30s tolerance, two such waits would exhaust the budget.
    delays = []
    for _ in range(8):
        await acct.refresh_now()
        delays.append(acct._retry_delay())
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert all(d == 10.0 for d in delays[4:])  # capped at the normal interval


async def test_recovery_resets_the_ladder():
    broker = _Boom(RuntimeError("502"), fail_times=2)
    acct = _account(FakeClock(), broker, refresh_interval_s=10.0)
    assert await acct.refresh_now() is False
    assert await acct.refresh_now() is False
    assert acct.consecutive_refresh_failures == 2
    assert await acct.refresh_now() is True
    assert acct.consecutive_refresh_failures == 0
    assert acct._retry_delay() == 1.0


async def test_expired_token_triggers_reauth_not_a_blind_retry():
    # Observed live: "token_invalid: This token is invalid or has expired".
    # Retrying the same dead token just burns the staleness budget.
    token_error = RuntimeError("token_invalid: This token is invalid or has expired")
    broker = _Boom(token_error, fail_times=1)
    reauths = []

    async def reauth():
        reauths.append(1)

    acct = _account(FakeClock(), broker, refresh_interval_s=0.01, reauth=reauth)
    task = asyncio.create_task(acct._refresh_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert reauths, "an auth error must re-mint the token"
    assert acct.consecutive_refresh_failures == 0  # and then recover


async def test_transient_failure_does_not_reauth():
    reauths = []

    async def reauth():
        reauths.append(1)

    acct = _account(FakeClock(), _Boom(RuntimeError("502 Bad Gateway"), fail_times=1),
                    refresh_interval_s=0.01, reauth=reauth)
    task = asyncio.create_task(acct._refresh_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert reauths == []  # a 502 is not a credential problem


async def test_blind_window_is_recorded_in_telemetry():
    # The failures used to exist only in stderr, so the audit log showed gate
    # denials with no recorded cause.
    events = []
    broker = _Boom(RuntimeError("502 Bad Gateway"), fail_times=1)
    clock = FakeClock(0.0)
    acct = _account(clock, broker, stale_after_s=5.0, telemetry=lambda c, p: events.append((c, p)))
    acct.seed([], Balances(net_liquidating_value=100_000.0))

    clock.t = 20.0
    assert await acct.refresh_now() is False
    category, payload = events[0]
    assert category == "account.refresh_failed"
    assert payload["consecutive_failures"] == 1
    assert payload["age_s"] == 20.0
    assert payload["blocking_entries"] is True  # age 20s > 5s tolerance
    assert payload["auth_error"] is False

    assert await acct.refresh_now() is True
    assert events[1][0] == "account.refresh_recovered"
    assert events[1][1]["consecutive_failures"] == 1


async def test_the_port_can_attribute_a_failure_to_one_leg():
    """`leg` / `error_class` come from the port; the caller records them verbatim.

    Reading them off attributes rather than off `type(exc).__name__` is the whole
    point: an httpx or SDK release that renames a class must not silently break
    every audit query written against this record.
    """
    from argus.account.port import tag_error

    events = []
    exc = tag_error(httpx2.ConnectError("connection refused"),
                    leg="balances", error_class="transport")
    acct = _account(FakeClock(), _Boom(exc), telemetry=lambda c, p: events.append((c, p)))
    assert await acct.refresh_now() is False

    payload = events[0][1]
    assert payload["leg"] == "balances"
    assert payload["error_class"] == "transport"
    assert payload["error_type"] == "ConnectError"   # UNCHANGED from before
    assert "elapsed_s" in payload


async def test_an_auth_error_outranks_whatever_class_the_port_declared():
    """A 401 arrives as a `TastytradeError` tagged `http` — the message is the split.

    Resolving it in one place is what keeps `error_class` and `auth_error`
    consistent by construction instead of by two call sites agreeing.
    """
    from argus.account.port import tag_error

    events = []
    exc = tag_error(
        RuntimeError("token_invalid: This token is invalid or has expired"),
        leg="positions", error_class="http",
    )
    acct = _account(FakeClock(), _Boom(exc), telemetry=lambda c, p: events.append((c, p)))
    assert await acct.refresh_now() is False

    payload = events[0][1]
    assert payload["error_class"] == "auth"
    assert payload["auth_error"] is True


async def test_an_untagged_port_failure_still_records_something_useful():
    """A fake, or a future port, that declares nothing must not produce nulls."""
    events = []
    acct = _account(FakeClock(), _Failing(RuntimeError("link down")),
                    telemetry=lambda c, p: events.append((c, p)))
    assert await acct.refresh_now() is False

    payload = events[0][1]
    assert payload["leg"] is None
    assert payload["error_class"] == "other"


async def test_telemetry_failure_cannot_break_the_refresh():
    def _explode(category, payload):
        raise RuntimeError("audit is down")

    acct = _account(FakeClock(), FakeBrokerAccount([], net_liquidating_value=100_000.0),
                    telemetry=_explode)
    assert await acct.refresh_now() is True


async def test_auth_error_classification():
    assert is_auth_error(RuntimeError("token_invalid: This token is invalid or has expired"))
    assert is_auth_error(RuntimeError("Unauthorized"))
    assert not is_auth_error(RuntimeError("502 Bad Gateway"))
    assert not is_auth_error(TimeoutError())


# -- stream connection ownership -------------------------------------------
#
# Same 2026-07-21 failure mode as the DXLink adapter (see
# tests/test_dxlink_reconnect.py): the streamer was entered in start()'s task
# and exited in stop()'s, which anyio forbids, and nothing reconnected.


class FakeAlertStreamer:
    instances: list["FakeAlertStreamer"] = []

    def __init__(self, session, *, fail_after=None, enter_error=None):
        self.accounts = []
        self.entered_in = None
        self.exited_in = None
        self._fail_after = fail_after
        self._enter_error = enter_error
        FakeAlertStreamer.instances.append(self)

    async def __aenter__(self):
        if self._enter_error is not None:
            raise self._enter_error
        self.entered_in = asyncio.current_task()
        return self

    async def __aexit__(self, *exc):
        self.exited_in = asyncio.current_task()
        return False

    async def subscribe_accounts(self, accounts):
        self.accounts = list(accounts)

    async def listen(self, event_type):
        from tastytrade.account import CurrentPosition

        if self._fail_after is not None and event_type is CurrentPosition:
            await asyncio.sleep(self._fail_after)
            raise ConnectionError("WebSocketNetworkError")
        await asyncio.Event().wait()
        yield  # pragma: no cover


def _streaming_account(factory, **kw):
    kw.setdefault("reconnect_backoff_s", 0.01)
    kw.setdefault("reconnect_backoff_max_s", 0.02)
    return AlertStreamerAccount(
        object(), object(), clock=FakeClock(), streamer_factory=factory, **kw
    )


async def test_alert_streamer_entered_and_exited_by_the_same_task():
    FakeAlertStreamer.instances.clear()
    acct = _streaming_account(FakeAlertStreamer)
    await acct.start()
    await acct.stop()
    streamer = FakeAlertStreamer.instances[0]
    assert streamer.entered_in is not None
    assert streamer.entered_in is streamer.exited_in


async def test_alert_streamer_reconnects_and_resubscribes():
    FakeAlertStreamer.instances.clear()
    acct = _streaming_account(lambda s: FakeAlertStreamer(s, fail_after=0.01))
    await acct.start()
    await asyncio.sleep(0.1)
    assert acct.reconnects >= 1
    # The account subscription is re-established, not silently lost.
    assert FakeAlertStreamer.instances[1].accounts
    await acct.stop()


async def test_liveness_refresh_survives_a_stream_reconnect():
    # The REST re-seed is exactly what keeps account state usable while the
    # stream is down, so it must NOT be torn down with the streamer.
    FakeAlertStreamer.instances.clear()
    broker = FakeBrokerAccount([], net_liquidating_value=100_000.0)
    acct = _streaming_account(
        lambda s: FakeAlertStreamer(s, fail_after=0.01),
        refresher=broker, refresh_interval_s=0.01,
    )
    await acct.start()
    await asyncio.sleep(0.1)
    assert acct.reconnects >= 1
    refresh = [t for t in acct._tasks if t.get_name() == "account-liveness-refresh"]
    assert refresh and not refresh[0].done()
    await acct.stop()


async def test_alert_streamer_first_connect_failure_is_fatal():
    FakeAlertStreamer.instances.clear()
    acct = _streaming_account(
        lambda s: FakeAlertStreamer(s, enter_error=RuntimeError("bad token"))
    )
    with pytest.raises(RuntimeError, match="bad token"):
        await acct.start()
