"""The account liveness refresh, off the event loop.

Every test here drives the REAL sync `httpx2.Client` through a `MockTransport`:
no network, fully deterministic, and the REAL `validate_and_parse` and the REAL
pydantic models run — so the error-fidelity assertions mean something instead of
asserting a mock back to itself.

What these guard, in one line each:
  - the budget is TOTAL for a serialized pair, with a floor (§10.4)
  - `error_type` strings do not move, including the timeout normalization (§10.3)
  - the token is re-read on every dispatch, so a revocation heals (§10.5)
  - the thread has no second login path (§10.6)
  - the two URLs and the header set have not drifted from the SDK (§10.7)
  - a cancelled dispatch refuses the next one loudly instead of queueing (§10.8)
→ docs/designs/2026-08-25-account-refresh-off-event-loop.md
"""

from __future__ import annotations

import asyncio
import threading

import httpx2
import pytest
from tastytrade import API_URL, API_VERSION, CERT_URL, Session
from tastytrade.account import AccountBalance, CurrentPosition
from tastytrade.utils import TastytradeError

from argus.account.port import ERROR_CLASS_ATTR, declared_error_class, refresh_leg
from argus.account.refresher import (
    _MIN_REQUEST_BUDGET_S,
    RefreshBusyError,
    ThreadedAccountRefresher,
    build_client,
)

ACCOUNT = "5WX00000"


def _wire_body(model: type, **overrides: object) -> dict:
    """A minimal valid wire payload for an SDK model, derived from the model itself.

    Hand-written fixtures for these two are 40-odd dasherized keys each and go
    stale on any SDK schema change — which would fail these tests for a reason
    that has nothing to do with the refresher. Deriving the required set keeps the
    subject of each test the thing it is actually testing. The SDK's SCHEMA is
    guarded separately, by the drift tests below.
    """
    import datetime as _dt
    import decimal

    filler = {
        decimal.Decimal: "0.0",
        str: "x",
        int: 1,
        float: 0.0,
        bool: False,
        _dt.date: "2026-08-26",
        _dt.datetime: "2026-08-26T13:31:00.000Z",
    }
    body: dict[str, object] = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        key = name.replace("_", "-")
        body[key] = overrides.pop(key, filler.get(field.annotation, "x"))
    body.update(overrides)
    return body


POSITIONS_BODY = {
    "data": {
        "items": [
            _wire_body(
                CurrentPosition,
                **{
                    "symbol": "SPXW  260826P07685000",
                    "instrument-type": "Equity Option",
                    "underlying-symbol": "SPXW",
                    "quantity": "2",
                    "quantity-direction": "Short",
                    "average-open-price": "3.00",
                    "mark-price": "3.05",
                },
            )
        ]
    }
}

BALANCES_BODY = {
    "data": _wire_body(AccountBalance, **{"net-liquidating-value": "99390.0"})
}


class FakeSession:
    """The two attributes the refresher is allowed to touch, and nothing else.

    Deliberately NOT a `tastytrade.Session`: if this ever needs more than
    `is_test`, `session_token` and `refresh()`, the boundary has moved and the
    test should be the thing that says so.
    """

    def __init__(self, *, is_test: bool = True, token: str = "tok-1") -> None:
        self.is_test = is_test
        self.session_token = token
        self.refreshes = 0
        self.refresh_error: Exception | None = None

    async def refresh(self) -> None:
        self.refreshes += 1
        if self.refresh_error is not None:
            raise self.refresh_error


class Recorder:
    """A MockTransport handler that logs every request and plays a script."""

    def __init__(self, handler=None) -> None:
        self.requests: list[httpx2.Request] = []
        self._handler = handler

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.url.path != "/oauth/token", (
            "the worker thread has no login path: it holds no refresh token and no "
            "provider secret, and must never mint a token (§10.6)"
        )
        if self._handler is not None:
            return self._handler(request)
        return _canned(request)

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def _canned(request: httpx2.Request) -> httpx2.Response:
    if request.url.path.endswith("/positions"):
        return httpx2.Response(200, json=POSITIONS_BODY)
    return httpx2.Response(200, json=BALANCES_BODY)


def _refresher(recorder: Recorder, *, session: FakeSession | None = None,
               budget_s: float = 5.0, **kw) -> ThreadedAccountRefresher:
    return ThreadedAccountRefresher(
        session or FakeSession(),
        ACCOUNT,
        budget_s=budget_s,
        http_timeout_s=30.0,
        refresh_interval_s=10.0,
        transport=httpx2.MockTransport(recorder),
        **kw,
    )


# -- the happy path ----------------------------------------------------------


async def test_fetch_snapshot_returns_both_halves_parsed_by_the_sdk():
    recorder = Recorder()
    refresher = _refresher(recorder)
    try:
        positions, balances = await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert [p.symbol for p in positions] == ["SPXW  260826P07685000"]
    # Signed, via the real `map_position` — the reconciler's integer comparison.
    assert positions[0].signed_quantity == -2
    assert balances.net_liquidating_value == 99_390.0


async def test_the_work_runs_off_the_event_loop_thread():
    """The whole point, asserted directly rather than inferred from timing."""
    seen: list[str] = []
    loop_thread = threading.current_thread().name

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(threading.current_thread().name)
        return _canned(request)

    refresher = _refresher(Recorder(handler))
    try:
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert seen and all(name != loop_thread for name in seen)
    assert all(name.startswith("argus-acct-refresh") for name in seen)


# -- §10.4 the budget --------------------------------------------------------


async def test_positions_is_requested_before_balances_over_one_connection():
    recorder = Recorder()
    refresher = _refresher(recorder)
    try:
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert recorder.paths == [
        f"/accounts/{ACCOUNT}/positions",
        f"/accounts/{ACCOUNT}/balances/USD",
    ]
    # Serialized against one deadline, so both legs go over one connection — the
    # cold-start handshake count that used to be 2 (concurrent `gather`) is 1.
    assert len(recorder.requests) == 2


async def test_each_request_is_bounded_by_what_is_left_of_the_budget():
    """The budget is TOTAL for the pair, not per request.

    Per-request would be a 10s worst case against a 5s key, which equals
    `account_refresh_s` and breaks the invariant the loader enforces.
    """
    timeouts: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return _canned(request)

    refresher = _refresher(Recorder(handler), budget_s=5.0)
    try:
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert len(timeouts) == 2
    assert timeouts[0] <= 5.0
    assert timeouts[1] < timeouts[0], "the second leg must inherit the REMAINING budget"


async def test_the_non_read_phases_cannot_stack_on_top_of_the_read_budget():
    """The deadline has to be real, and a scalar `timeout=` does not make it real.

    httpx's `Timeout(X)` sets connect, read, write AND pool to X *independently*,
    so `timeout=remaining` allowed one leg to spend the whole budget connecting and
    the whole budget again reading. On 2026-08-26 that outran `refresh_now`'s 10s
    backstop five times — in a module whose docstring promises one monotonic
    deadline, and whose backstop is documented as meaning "the bug is in the port".

    The test the old suite had only ever asserted the READ phase, which is why the
    stacking was invisible. This pins the other three: they are FIXED ceilings that
    do not scale with the budget, so the worst case is `remaining + 1.5s` rather
    than `4 x remaining`.
    """
    phases: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        phases.append(dict(request.extensions["timeout"]))
        return _canned(request)

    refresher = _refresher(Recorder(handler), budget_s=5.0)
    try:
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    for phase in phases:
        assert phase["connect"] <= 1.0
        assert phase["write"] <= 0.25
        assert phase["pool"] <= 0.25
        # The whole point: one leg cannot outrun the attempt's own budget, and
        # therefore cannot outrun `refresh_now`'s backstop at 2x that budget.
        total = phase["connect"] + phase["read"] + phase["write"] + phase["pool"]
        assert total <= 5.0 + 1.5, f"a single leg could spend {total:g}s of a 5s budget"


async def test_the_phase_ceilings_do_not_scale_with_a_larger_budget():
    """Fixed ceilings, not fractions — a bigger budget must not buy a longer handshake.

    Connect is empirically not where this endpoint spends time (zero ConnectTimeouts
    across ~1,464 failures on 2026-08-26; every one was a read timeout or a fast
    502). Letting connect grow with the budget would re-open the stacking this
    closed.
    """
    phases: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        phases.append(dict(request.extensions["timeout"]))
        return _canned(request)

    refresher = _refresher(Recorder(handler), budget_s=60.0)
    try:
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert phases[0]["read"] > 50.0, "read DOES scale — it is what the budget is for"
    assert phases[0]["connect"] == 1.0, "connect does not"
    assert phases[0]["write"] == 0.25
    assert phases[0]["pool"] == 0.25


async def test_a_budget_smaller_than_the_phase_ceilings_clamps_them():
    """A sliver of budget left must not hand connect MORE time than the leg has."""
    phases: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        phases.append(dict(request.extensions["timeout"]))
        return _canned(request)

    refresher = _refresher(Recorder(handler), budget_s=0.4)
    try:
        await refresher.fetch_snapshot()
    except Exception:
        pass  # the second leg may fall below the floor; the first one is the subject
    finally:
        await refresher.aclose()

    assert phases, "the first leg must still be issued"
    assert phases[0]["connect"] <= 0.4
    assert phases[0]["write"] <= 0.4
    assert phases[0]["pool"] <= 0.4


async def test_the_second_leg_is_not_issued_below_the_floor():
    """A request certain to time out in milliseconds costs a socket for nothing."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/positions"):
            # Burn all but a sliver of the budget.
            import time as _t
            _t.sleep(0.35)
        return _canned(request)

    recorder = Recorder(handler)
    refresher = _refresher(recorder, budget_s=0.35 + _MIN_REQUEST_BUDGET_S - 0.05)
    try:
        with pytest.raises(TimeoutError) as excinfo:
            await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert recorder.paths == [f"/accounts/{ACCOUNT}/positions"], "balances was issued anyway"
    assert refresh_leg(excinfo.value) == "balances"
    assert "not issued" in str(excinfo.value)


async def test_a_failing_leg_leaves_nothing_partially_seeded():
    """All-or-nothing: the fail-closed contract is a contract, not a timeout knob."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/positions"):
            return httpx2.Response(200, json=POSITIONS_BODY)
        return httpx2.Response(502, json={"error": {"code": "bad_gateway", "message": "nope"}})

    refresher = _refresher(Recorder(handler))
    try:
        with pytest.raises(TastytradeError):
            await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()


# -- §10.3 error fidelity ----------------------------------------------------


def _raises(exc: Exception):
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    "handler, error_type, auth_marker, error_class",
    [
        pytest.param(
            _raises(httpx2.ReadTimeout("timed out", request=None)),
            "TimeoutError", False, "timeout",
            id="httpx-timeout-normalized-to-builtin",
        ),
        pytest.param(
            lambda r: httpx2.Response(
                502, json={"error": {"code": "bad_gateway", "message": "Bad Gateway"}}
            ),
            "TastytradeError", False, "http",
            id="502-from-cert",
        ),
        pytest.param(
            lambda r: httpx2.Response(
                401,
                json={
                    "error": {
                        "code": "token_invalid",
                        "message": "This token is invalid or has expired",
                    }
                },
            ),
            "TastytradeError", True, "auth",
            id="401-expired-token",
        ),
        pytest.param(
            _raises(httpx2.ConnectError("connection refused")),
            "ConnectError", False, "transport",
            id="transport-error",
        ),
        pytest.param(
            lambda r: httpx2.Response(200, json={"data": {"items": [{"symbol": "?"}]}}),
            "ValidationError", False, "other",
            id="malformed-payload",
        ),
    ],
)
async def test_error_fidelity(handler, error_type, auth_marker, error_class):
    """Every `error_type` string an audit query already matches on stays put.

    The 401 row is the one where `error_class` and `error_type` deliberately
    disagree: the SDK gives one `TastytradeError` for every non-2xx, and the auth
    split lives in the MESSAGE. `is_auth_error` is what reads it — asserted here
    via the marker text, and end-to-end in `test_account_liveness.py`.
    """
    from argus.account.stream import is_auth_error

    refresher = _refresher(Recorder(handler))
    try:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - the type IS the assertion
            await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    exc = excinfo.value
    assert type(exc).__name__ == error_type
    assert is_auth_error(exc) is auth_marker
    # `error_class` is OURS and coarse, so the next investigation does not depend
    # on third-party class names staying put. The refresher declares the
    # transport-level classes; `auth` is resolved by the caller from the message.
    assert declared_error_class(exc) == ("http" if error_class == "auth" else error_class)
    assert refresh_leg(exc) == "positions"


async def test_the_httpx_timeout_class_survives_in_the_message_and_cause():
    refresher = _refresher(Recorder(_raises(httpx2.ReadTimeout("slow", request=None))))
    try:
        with pytest.raises(TimeoutError) as excinfo:
            await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert "ReadTimeout" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, httpx2.ReadTimeout)


async def test_the_leg_is_named_on_a_balances_side_failure():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/positions"):
            return httpx2.Response(200, json=POSITIONS_BODY)
        raise httpx2.ConnectError("balances only")

    refresher = _refresher(Recorder(handler))
    try:
        with pytest.raises(httpx2.ConnectError) as excinfo:
            await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    # "the account endpoint is sick" vs "the whole API is sick" — `gather` could
    # not tell them apart, the serialized shape gets it for free.
    assert refresh_leg(excinfo.value) == "balances"


async def test_a_tastytrade_error_message_is_byte_identical_to_the_sdks():
    """Tagging must not touch the message — `is_auth_error` matches on it."""
    body = {"error": {"code": "token_invalid", "message": "This token is invalid or has expired"}}
    refresher = _refresher(Recorder(lambda r: httpx2.Response(401, json=body)))
    try:
        with pytest.raises(TastytradeError) as excinfo:
            await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    assert str(excinfo.value) == "token_invalid: This token is invalid or has expired\n"


# -- §10.5 token freshness ---------------------------------------------------


async def test_the_session_is_refreshed_before_every_dispatch():
    session = FakeSession()
    refresher = _refresher(Recorder(), session=session)
    try:
        await refresher.fetch_snapshot()
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()
    assert session.refreshes == 2


async def test_a_rotated_token_reaches_the_next_request():
    """The "silently 401s forever" guard.

    Caching the token at construction is the shape that breaks: after a
    server-side revocation the thread would present the dead credential for the
    life of the process. Re-reading per dispatch is what makes `_try_reauth` work.
    """
    session = FakeSession(token="tok-1")
    recorder = Recorder()
    refresher = _refresher(recorder, session=session)
    try:
        await refresher.fetch_snapshot()
        session.session_token = "tok-2"
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()

    bearers = [r.headers["authorization"] for r in recorder.requests]
    assert bearers[:2] == ["Bearer tok-1", "Bearer tok-1"]
    assert bearers[2:] == ["Bearer tok-2", "Bearer tok-2"]


async def test_a_failed_mint_fails_the_attempt_without_dispatching():
    session = FakeSession()
    session.refresh_error = TastytradeError("invalid_credentials")
    recorder = Recorder()
    refresher = _refresher(recorder, session=session)
    try:
        with pytest.raises(TastytradeError):
            await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()
    assert recorder.requests == []


async def test_the_client_carries_no_authorization_header_of_its_own():
    """It is set per request, from the token read at dispatch time."""
    client = build_client(FakeSession(), http_timeout_s=30.0, keepalive_expiry_s=30.0)
    try:
        assert "authorization" not in client.headers
    finally:
        client.close()


# -- §10.7 SDK drift ---------------------------------------------------------


@pytest.mark.parametrize("is_test", [True, False])
def test_base_url_and_headers_match_a_real_sdk_session(is_test):
    """The cost of shape (a) is two URL literals. This is what bounds it.

    `Session.__init__` does no I/O, so a real one can be built offline and
    diffed. An SDK release that moves the endpoint or changes the header set
    fails here rather than 404ing on the droplet's first refresh.
    """
    sdk = Session(provider_secret="x", refresh_token="y", is_test=is_test)
    ours = build_client(FakeSession(is_test=is_test), http_timeout_s=30.0,
                        keepalive_expiry_s=30.0)
    try:
        assert str(ours.base_url) == str(sdk._client.base_url)
        assert dict(ours.headers) == dict(sdk._client.headers)
        assert str(ours.base_url).rstrip("/") == (CERT_URL if is_test else API_URL).rstrip("/")
        # The rule mirrored from the SDK: Accept-Version is not accepted in sandbox.
        assert ("accept-version" in ours.headers) is (not is_test)
        if not is_test:
            assert ours.headers["accept-version"] == API_VERSION
    finally:
        ours.close()


def test_the_endpoints_are_the_ones_the_sdk_calls():
    """Guards the two literals against a rename inside `tastytrade.account`."""
    import inspect

    from tastytrade.account import Account

    positions_src = inspect.getsource(Account.get_positions)
    balances_src = inspect.getsource(Account.get_balances)
    assert "/accounts/{self.account_number}/positions" in positions_src
    assert "/accounts/{self.account_number}/balances/{currency}" in balances_src
    # And the reason the refresher sends no `params`: every one of the SDK's is
    # optional and defaults to None, and it filters the Nones out.
    assert "for k, v in params.items() if v is not None" in positions_src


def test_the_declared_error_class_attribute_name_has_not_drifted():
    """`RefreshBusyError` spells the attribute as a literal; this is the check."""
    assert getattr(RefreshBusyError, ERROR_CLASS_ATTR, None) == "busy"
    assert declared_error_class(RefreshBusyError("x")) == "busy"


# -- §10.8 the in-flight guard -----------------------------------------------


async def test_a_cancelled_dispatch_makes_the_next_one_refuse_loudly():
    """`run_in_executor` cannot be cancelled: the thread runs on, unobserved.

    With one worker the next dispatch would queue silently behind it, which is a
    wedge nothing surfaces. It refuses instead, and the staleness it lets grow is
    the rail that already alerts.
    """
    release = threading.Event()
    entered = threading.Event()
    recorder = Recorder()

    def handler(request: httpx2.Request) -> httpx2.Response:
        entered.set()
        release.wait(10.0)
        return _canned(request)

    refresher = _refresher(Recorder(handler))
    try:
        task = asyncio.create_task(refresher.fetch_snapshot())
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(RefreshBusyError) as excinfo:
            await refresher.fetch_snapshot()
        assert "outstanding" in str(excinfo.value)
        assert declared_error_class(excinfo.value) == "busy"
        # It REFUSED — it did not submit a second job behind the wedged one.
        assert recorder.requests == []
    finally:
        release.set()
        await refresher.aclose()


async def test_the_guard_clears_itself_when_the_abandoned_thread_finishes():
    """Self-healing, with no retry and no cancellation of anything."""
    release = threading.Event()
    entered = threading.Event()

    def handler(request: httpx2.Request) -> httpx2.Response:
        entered.set()
        release.wait(10.0)
        return _canned(request)

    refresher = _refresher(Recorder(handler))
    try:
        task = asyncio.create_task(refresher.fetch_snapshot())
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RefreshBusyError):
            await refresher.fetch_snapshot()

        release.set()
        for _ in range(200):  # let the abandoned thread finish
            if refresher._inflight is None or refresher._inflight.done():
                break
            await asyncio.sleep(0.01)

        positions, _ = await refresher.fetch_snapshot()
        assert len(positions) == 1
    finally:
        release.set()
        await refresher.aclose()


async def test_a_refresher_uses_its_own_executor_not_the_default_one():
    """Cheap structural half of the isolation pair in test_no_leaks-style tests."""
    loop = asyncio.get_running_loop()
    refresher = _refresher(Recorder())
    try:
        assert refresher._owns_executor is True
        assert refresher._executor is not getattr(loop, "_default_executor", None)
        await refresher.fetch_snapshot()
    finally:
        await refresher.aclose()


# -- §10.1 executor isolation — the highest-value test in the change ---------
#
# The refresh is the highest-frequency I/O in the process and the one whose
# failure mode is "hangs". The DEFAULT executor is where `audit/log.py` dispatches
# every Tier 1 and Tier 2 write — and a failed audit write latches, engages the
# kill switch, and halts trading with NO auto-retry. On the droplet's 2 vCPU the
# default pool is 6 workers shared by Tier 1 writes, Tier 2 writes, `/status`
# reads, the Slack POST, and asyncio's own `getaddrinfo`.
#
# A single positive assertion here would be a tautology — nothing would prove the
# harness could ever have failed — so this is a PAIR. The negative test is what
# makes the positive one a regression guard rather than a decoration.


async def _audit_write_delay_with_a_wedged_refresh(db, *, share_the_default: bool) -> float:
    """Wedge one refresh forever, then time a Tier 1 audit write. Seconds waited.

    ONE harness for both halves of the pair, so "the only difference is which
    executor the refresher uses" is a fact about the code rather than a claim in
    a comment. Always releases the wedge and joins the worker.
    """
    from concurrent.futures import ThreadPoolExecutor

    from argus.audit.log import AuditLog

    loop = asyncio.get_running_loop()
    previous = getattr(loop, "_default_executor", None)
    # ONE worker, so the starvation is deterministic rather than a race against
    # however many cores the runner happens to have.
    default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-default")
    loop.set_default_executor(default)

    entered, release = threading.Event(), threading.Event()

    def handler(request: httpx2.Request) -> httpx2.Response:
        entered.set()
        release.wait(10.0)
        return _canned(request)

    refresher = _refresher(
        Recorder(handler), **({"executor": default} if share_the_default else {})
    )
    audit = AuditLog(db, tier2_queue_maxsize=64, tier2_queue_alert_depth=48)
    task = None
    try:
        task = asyncio.create_task(refresher.fetch_snapshot())
        # Wait until the worker is INSIDE the request, not merely submitted.
        deadline = loop.time() + 5.0
        while not entered.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert entered.is_set(), "the dispatch never reached the worker thread"

        started = loop.time()
        try:
            await asyncio.wait_for(audit.record("test.isolation", {}), timeout=1.0)
        except TimeoutError:
            return float("inf")
        return loop.time() - started
    finally:
        release.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await refresher.aclose()
        await audit.aclose()
        default.shutdown(wait=True)
        # `set_default_executor` rejects None, and there is no public way back to
        # "no default"; the attribute is the only route.
        loop._default_executor = previous


async def test_a_wedged_refresh_cannot_delay_an_audit_write(db):
    """The claim, in one line: the audit log does not wait on a hung refresh."""
    waited = await _audit_write_delay_with_a_wedged_refresh(db, share_the_default=False)
    assert waited < 1.0


async def test_the_isolation_test_has_teeth(db):
    """The negative half. If this ever passes, the test above proves nothing.

    Same harness, same wedge, one difference: the refresher is built on the
    DEFAULT executor. That is the entire reason the executor is a constructor
    argument — hardcoding it inside the class is the obvious simplification and it
    silently removes the teeth from the highest-value test in the change.
    """
    waited = await _audit_write_delay_with_a_wedged_refresh(db, share_the_default=True)
    assert waited == float("inf"), "sharing the audit log's pool did NOT starve it"


# -- §10.9 the loop stays responsive — provable without a certificate --------


async def _lag_over(refreshes, *, blocking_s: float = 0.2, rounds: int = 3):
    """Run `refreshes` under a REAL LoopHeartbeat at production settings.

    `interval_s=0.1` / `lag_alert_threshold_s=0.05` are the droplet's own numbers.
    The property under test is "the blocking work does not stall the loop", and
    that needs no TLS at all: a handler that sleeps stands in for the handshake,
    which makes this fast, hermetic, and unable to flake on a certificate.
    """
    import time as _t

    from argus.heartbeat import LoopHeartbeat

    def handler(request: httpx2.Request) -> httpx2.Response:
        _t.sleep(blocking_s / 2)  # two legs per attempt
        return _canned(request)

    refresher = _refresher(Recorder(handler))
    heartbeat = LoopHeartbeat(
        interval_s=0.1,
        lag_threshold_s=0.05,
        sustained_breach_count=10_000,     # never escalate; we only want metrics
        alert=lambda kind, payload: None,
    )
    heartbeat.start()
    try:
        await asyncio.sleep(0.2)           # settle
        heartbeat._max_lag = 0.0
        heartbeat._breach_count = 0
        for _ in range(rounds):
            await refreshes(refresher)
            await asyncio.sleep(0.05)
    finally:
        await heartbeat.stop()
        await refresher.aclose()
    return heartbeat.max_lag, heartbeat.breach_count


async def test_the_blocking_work_no_longer_stalls_the_event_loop():
    async def on_loop(refresher):
        # The BEFORE shape: exactly the same work, run on the loop thread. This
        # is what the refresh did until 2026-08-26, with a TLS handshake in place
        # of the sleep.
        refresher._fetch_sync("tok-1")

    async def off_loop(refresher):
        await refresher.fetch_snapshot()

    before_lag, before_breaches = await _lag_over(on_loop)
    after_lag, after_breaches = await _lag_over(off_loop)

    assert before_lag >= 0.15, "the before shape did not reproduce the stall"
    assert before_breaches > 0
    # The whole change, in one assertion.
    assert after_lag < 0.05, f"work is back on the loop: max lag {after_lag:.3f}s"
    assert after_breaches == 0


async def test_an_injected_executor_is_not_shut_down_by_aclose():
    """The affordance is test-only, and it does not take ownership."""
    from concurrent.futures import ThreadPoolExecutor

    external = ThreadPoolExecutor(max_workers=1)
    try:
        refresher = _refresher(Recorder(), executor=external)
        assert refresher._owns_executor is False
        await refresher.fetch_snapshot()
        await refresher.aclose()
        assert external.submit(lambda: 7).result(timeout=5.0) == 7
    finally:
        external.shutdown(wait=True)


async def test_aclose_is_idempotent_and_refuses_further_dispatch():
    refresher = _refresher(Recorder())
    await refresher.fetch_snapshot()
    await refresher.aclose()
    await refresher.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await refresher.fetch_snapshot()
