"""Event-loop lag under the account liveness refresh — the ON-loop vs OFF-loop measurement.

The hermetic test (`tests/test_account_refresher.py`) proves the blocking work is
off the loop, using a handler that sleeps. It cannot produce the PRODUCTION
NUMBERS: a `MockTransport` has no certificate chain and no WAN RTT, and the cost
being removed is a **TLS handshake**. This is the manual instrument that measures
it, in the same class as `tools/evaluability.py` — a measurement that cannot be a
unit test.

    ./.venv/bin/python -m argus.tools.loop_lag --config argus.dev.toml
    ./.venv/bin/python -m argus.tools.loop_lag --config argus.dev.toml --live-cert

Default target is a **local TLS stub** with its own self-signed certificate,
reproducing the 2026-08-25 harness: no network, no credentials, and a handshake
on every cold connection. `--live-cert` points the same two shapes at the cert
API using the read-only GET pair, which adds real RTT and a real chain.

Opens **no database at all** — nothing here writes, and there is nothing to read.
`--config` is still required (CLAUDE.md): it supplies the staleness and broker
numbers so the shapes are measured at the settings that actually run.

**WHAT THIS DOES AND DOES NOT SETTLE.** It settles that the handshake's CPU cost
moved off the loop. It does not settle the production acceptance question, which
is answered from the droplet over a FULL SESSION:

    did `LoopHeartbeat.breach_count` fall WHILE `account.refresh_failed` volume
    stayed comparable?

"Did the pool stay warm?" is the wrong question and will read as a failure — the
pool still empties during a storm, deliberately and unavoidably. Read the session
number against `would_submit today > 0` and the blackout file, and against
process-lifetime rather than session-lifetime heartbeat counters, per the
2026-08-10 lag-ramp record's own two traps.
→ docs/designs/2026-08-25-account-refresh-off-event-loop.md
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx2

from argus.account.refresher import ThreadedAccountRefresher
from argus.bootstrap import load_env
from argus.config import Config
from argus.heartbeat import LoopHeartbeat

#: The droplet's own heartbeat settings. Measuring at anything else measures a
#: different detector.
_HEARTBEAT_INTERVAL_S = 0.1
_HEARTBEAT_THRESHOLD_S = 0.05

_STUB_ACCOUNT = "5WX00000"


# -- the local TLS stub ------------------------------------------------------


def _minimal_body(model: type, **overrides: Any) -> dict[str, Any]:
    """A minimal VALID wire payload for an SDK model, derived from the model.

    The refresher runs the SDK's real parsing, so the stub has to answer with
    something the real `CurrentPosition`/`AccountBalance` accept — 40-odd
    dasherized keys each. Deriving the required set from the model beats pasting
    them, and beats measuring a run in which every attempt fails on a
    `ValidationError` instead of on the transport.
    """
    import datetime as _dt
    import decimal

    filler: dict[Any, Any] = {
        decimal.Decimal: "0.0",
        str: "x",
        int: 1,
        float: 0.0,
        bool: False,
        _dt.date: "2026-01-01",
        _dt.datetime: "2026-01-01T00:00:00.000Z",
    }
    body: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if field.is_required():
            body[name.replace("_", "-")] = filler.get(field.annotation, "x")
    body.update(overrides)
    return body


def _stub_bodies() -> tuple[bytes, bytes]:
    from tastytrade.account import AccountBalance, CurrentPosition

    positions = {
        "data": {
            "items": [
                _minimal_body(
                    CurrentPosition,
                    **{
                        "symbol": "SPXW  260826P07685000",
                        "instrument-type": "Equity Option",
                        "underlying-symbol": "SPXW",
                        "quantity": "2",
                        "quantity-direction": "Short",
                        "average-open-price": "3.00",
                    },
                )
            ]
        }
    }
    balances = {"data": _minimal_body(AccountBalance, **{"net-liquidating-value": "100000.0"})}
    return json.dumps(positions).encode(), json.dumps(balances).encode()


class _TlsStub:
    """A self-signed HTTPS server that answers the two account endpoints.

    Counts ACCEPTED CONNECTIONS, which on this server is the handshake count —
    the second number the measurement needs, because the shapes differ in how
    many connections they open as much as in where the cost lands.
    """

    def __init__(self, *, keepalive: bool = False) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="argus-loop-lag-"))
        # COLD BY DEFAULT, driven from the server side (`Connection: close`) so
        # neither client needs a special build. A cold pool is not the pessimistic
        # case here, it is the PERMANENT state during the refresh-failure storm
        # this change exists for: a httpx timeout destroys the connection it fired
        # on, so every subsequent attempt re-handshakes. Measuring warm measures
        # the easy day.
        self._keepalive = keepalive
        self.connections = 0
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def cert_path(self) -> Path:
        return self._dir / "cert.pem"

    def start(self) -> str:
        positions_body, balances_body = _stub_bodies()
        self._make_cert()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib's spelling
                body = positions_body if self.path.endswith("/positions") else balances_body
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if not stub._keepalive:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

            def get_request(self):  # type: ignore[override]
                stub.connections += 1
                return super().get_request()

        server = Server(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert_path, self._dir / "key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        host, port = server.server_address[:2]
        return f"https://{host}:{port}"

    def _make_cert(self) -> None:
        if shutil.which("openssl") is None:
            raise SystemExit(
                "openssl is not on PATH, and this tool needs it to mint the stub's "
                "self-signed certificate (the runtime is stdlib + the tastytrade SDK, "
                "so there is no `cryptography` to do it in-process).\n"
                "Use --live-cert to measure against the cert API instead."
            )
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(self._dir / "key.pem"),
                "-out", str(self.cert_path),
                "-days", "1", "-nodes", "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1",
            ],
            check=True, capture_output=True,
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        shutil.rmtree(self._dir, ignore_errors=True)


# -- measurement -------------------------------------------------------------


class _DenseSampler:
    """A high-resolution lag sampler, running BESIDE the real heartbeat.

    **Why both.** `LoopHeartbeat` at production settings is the thing whose
    `breach_count` is the production acceptance number, so it runs here at
    exactly those settings and its breach count is reported. But it samples every
    100ms against a 50ms threshold, and over a few hundred attempts that is a few
    hundred samples — it cannot RESOLVE a per-attempt effect of a few
    milliseconds, and reports 0/0 for both shapes whether or not a difference
    exists. That is a detector doing its job (it is an alarm, not a profiler) and
    a useless instrument.

    So the percentile columns come from a 1ms sampler measuring the same quantity
    — how much later than scheduled a wakeup fires, i.e. how long the loop was
    unavailable. Its own baseline (~0.15ms on a quiet loop) is visible in the p50
    column and should be read as the noise floor.
    """

    def __init__(self, interval_s: float) -> None:
        self.samples: list[float] = []
        self._interval = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="loop-lag-sampler")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        target = loop.time()
        while not self._stop.is_set():
            target += self._interval
            delay = target - loop.time()
            await asyncio.sleep(delay if delay > 0 else 0)
            self.samples.append(loop.time() - target)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task


def _pct(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


class _Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.samples: list[float] = []
        self.breaches = 0
        self.heartbeat_max = 0.0
        self.handshakes = 0
        self.attempts = 0
        self.failures = 0


async def _run_shape(name: str, attempt, n: int, stub: _TlsStub | None,
                     sample_s: float) -> _Result:
    result = _Result(name)
    heartbeat = LoopHeartbeat(
        interval_s=_HEARTBEAT_INTERVAL_S,
        lag_threshold_s=_HEARTBEAT_THRESHOLD_S,
        sustained_breach_count=10**9,   # never escalate; this is a measurement
        alert=lambda kind, payload: None,
    )
    sampler = _DenseSampler(sample_s)
    try:
        await attempt()                 # warm up: first-call caches, lazy imports
    except Exception:  # noqa: BLE001 - reported below if it repeats
        pass
    heartbeat.start()
    sampler.start()
    try:
        await asyncio.sleep(0.3)        # let the loop settle, then measure clean
        before_connections = stub.connections if stub is not None else 0
        sampler.samples.clear()
        heartbeat._max_lag = 0.0
        heartbeat._breach_count = 0
        for _ in range(n):
            result.attempts += 1
            try:
                await attempt()
            except Exception as exc:  # noqa: BLE001 - a failing attempt is data
                result.failures += 1
                if result.failures == 1:
                    print(f"  ({name}: first failure — {type(exc).__name__}: {exc})")
            await asyncio.sleep(0.02)
        result.samples = list(sampler.samples)
        result.breaches = heartbeat.breach_count
        result.heartbeat_max = heartbeat.max_lag
        if stub is not None:
            result.handshakes = stub.connections - before_connections
    finally:
        await sampler.stop()
        await heartbeat.stop()
    return result


def _report(results: list[_Result], *, handshakes_known: bool) -> None:
    print()
    print(f"{'shape':<10} {'p50':>9} {'p99':>9} {'max':>9} {'breaches':>9} "
          f"{'handshakes':>11} {'attempts':>9} {'failed':>7}")
    for r in results:
        hs = f"{r.handshakes}" if handshakes_known else "n/a"
        print(
            f"{r.name:<10} "
            f"{_pct(r.samples, 0.50) * 1000:>8.2f}ms "
            f"{_pct(r.samples, 0.99) * 1000:>8.2f}ms "
            f"{max(r.samples or [0.0]) * 1000:>8.2f}ms "
            f"{r.breaches:>9} {hs:>11} {r.attempts:>9} {r.failures:>7}"
        )
    print()
    print("p50/p99/max are the DENSE sampler; `breaches` is the real LoopHeartbeat at "
          f"the droplet's settings ({_HEARTBEAT_THRESHOLD_S * 1000:.0f}ms threshold, "
          f"{_HEARTBEAT_INTERVAL_S * 1000:.0f}ms cadence).")
    print("Read p50 as the sampler's own noise floor and p99 as the effect. Zero")
    print("breaches in BOTH rows is expected here and means only that this host's")
    print("handshake is nowhere near 50ms — not that the shapes are equivalent.")
    print()
    print("MAGNITUDE IS HOST- AND CHAIN-SPECIFIC, AND NOISY. A single self-signed")
    print("RSA-2048 cert over loopback is the CHEAPEST handshake there is: on a dev")
    print("Mac it separated the shapes by 1.6x to 5x at p99 across runs on")
    print("2026-08-26 — the right DIRECTION, well under the order of magnitude a")
    print("real chain on a vCPU shows. Use >=500 attempts, and read the direction")
    print("rather than any one run's multiple. --live-cert is the closer proxy.")
    print("See the module docstring for what this does NOT settle.")


# -- the two shapes ----------------------------------------------------------


class _StubSession:
    """`is_test` and a token, which is all `build_client` and the refresher read."""

    is_test = True
    session_token = "stub-token"

    async def refresh(self) -> None:
        return None


async def _measure_against_stub(config: Config, n: int, *, keepalive: bool,
                                sample_s: float) -> None:
    stub = _TlsStub(keepalive=keepalive)
    base_url = stub.start()
    print(f"local TLS stub at {base_url} (self-signed, {n} attempts per shape, "
          f"pool={'warm' if keepalive else 'COLD — the storm condition'})")

    session = _StubSession()
    # The SDK client's own keepalive, derived the way `broker/session.py` derives
    # it. NB the stub's `Connection: close` overrides this from the server side —
    # which is the point: a cold pool is the condition being measured.
    keepalive_expiry = config.staleness.account_refresh_s * 3.0

    # BEFORE: an async client on the event loop, firing both GETs CONCURRENTLY —
    # the shape that ran until 2026-08-26, handshakes and all.
    on_loop = httpx2.AsyncClient(
        base_url=base_url,
        verify=str(stub.cert_path),
        timeout=httpx2.Timeout(config.broker.http_timeout_s),
        limits=httpx2.Limits(max_connections=100, max_keepalive_connections=20,
                             keepalive_expiry=keepalive_expiry),
    )
    # AFTER: the real refresher, real parsing, on its own worker thread.
    off_loop = ThreadedAccountRefresher(
        session,
        _STUB_ACCOUNT,
        budget_s=config.staleness.account_refresh_timeout_s,
        http_timeout_s=config.broker.http_timeout_s,
        refresh_interval_s=config.staleness.account_refresh_s,
        base_url=base_url,
        verify=str(stub.cert_path),
    )

    async def before() -> None:
        async with asyncio.timeout(config.staleness.account_refresh_timeout_s):
            await asyncio.gather(
                on_loop.get(f"/accounts/{_STUB_ACCOUNT}/positions"),
                on_loop.get(f"/accounts/{_STUB_ACCOUNT}/balances/USD"),
            )

    try:
        results = [
            await _run_shape("on-loop", before, n, stub, sample_s),
            await _run_shape("off-loop", off_loop.fetch_snapshot, n, stub, sample_s),
        ]
    finally:
        await on_loop.aclose()
        await off_loop.aclose()
        stub.stop()
    _report(results, handshakes_known=True)
    if keepalive:
        print("NB --warm hides most of the effect: the cost being moved is the")
        print("HANDSHAKE, and a warm pool does not pay one. Run without --warm.")


async def _measure_against_cert(config: Config, n: int, account_number: str | None,
                                *, sample_s: float) -> None:
    from tastytrade import Account

    from argus.account.rest import BrokerAccount
    from argus.broker import TastytradeSession, trading_credentials

    if config.environment == "live":
        raise SystemExit("refusing to run the loop-lag instrument against a live session")

    tt = TastytradeSession(config, trading_credentials(config))
    print(f"connecting (is_test={tt.is_test}, env={config.environment}) ...")
    session = await tt.open()
    try:
        accounts = await Account.get(session)
        if account_number:
            accounts = [a for a in accounts if a.account_number == account_number]
        if not accounts:
            raise SystemExit("no matching account (see `python -m argus.tools.account`)")
        account = accounts[0]
        print(f"account: {account.account_number} ({n} attempts per shape)")

        broker = BrokerAccount(session, account)
        off_loop = ThreadedAccountRefresher(
            session,
            account.account_number,
            budget_s=config.staleness.account_refresh_timeout_s,
            http_timeout_s=config.broker.http_timeout_s,
            refresh_interval_s=config.staleness.account_refresh_s,
        )

        async def before() -> None:
            async with asyncio.timeout(config.staleness.account_refresh_timeout_s):
                await asyncio.gather(broker.fetch_positions(), broker.fetch_balances())

        try:
            results = [
                await _run_shape("on-loop", before, n, None, sample_s),
                await _run_shape("off-loop", off_loop.fetch_snapshot, n, None, sample_s),
            ]
        finally:
            await off_loop.aclose()
    finally:
        await tt.aclose()
    _report(results, handshakes_known=False)


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="path to a config file (REQUIRED, no default)"
    )
    parser.add_argument("--attempts", type=int, default=200,
                        help="refresh attempts per shape (default 200)")
    parser.add_argument("--sample-ms", type=float, default=1.0,
                        help="dense lag sampler interval in ms (default 1.0)")
    parser.add_argument("--warm", action="store_true",
                        help="let the stub keep connections alive (default is COLD, "
                             "which is what a refresh-failure storm actually looks like)")
    parser.add_argument("--live-cert", action="store_true",
                        help="measure against the cert API instead of a local TLS stub "
                             "(read-only GETs; places no orders)")
    parser.add_argument("--account", default=None, help="account number, with --live-cert")
    args = parser.parse_args()

    config = Config.load(args.config)
    # Deliberately no database — nothing here writes, and there is nothing to read.
    started = time.monotonic()
    if args.live_cert:
        asyncio.run(_measure_against_cert(config, args.attempts, args.account,
                                          sample_s=args.sample_ms / 1000.0))
    else:
        asyncio.run(_measure_against_stub(config, args.attempts, keepalive=args.warm,
                                          sample_s=args.sample_ms / 1000.0))
    print(f"(took {time.monotonic() - started:.1f}s)")


if __name__ == "__main__":
    main()
