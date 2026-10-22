"""Kill-switch HTTP control surface (spec §6): auth, the trip, idempotency, and a
real end-to-end request over loopback.

Every test drives the server through an ACTUAL TCP socket (`asyncio.open_connection`)
— there is no in-process shortcut — so the minimal HTTP reader/writer is exercised
exactly as a phone's curl would exercise it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.control import (
    KILL_TOKEN_VAR,
    TRIP_SOURCE,
    ControlServer,
    ControlServerBindError,
    MissingKillTokenError,
    load_kill_token,
)
from argus.db import apply_migrations, connect
from argus.audit.log import AuditLog
from argus.risk.killswitch import KillSwitch

# A distinctive token so tests can assert it appears in NO log line, response, or
# audit record.
TOKEN = "SUPERSECRET-kill-token-value"


@contextlib.asynccontextmanager
async def harness(tmp_path: Path, *, token: str = TOKEN):
    conn = connect(tmp_path / "argus.db")
    apply_migrations(conn)
    audit = AuditLog(conn, tier2_queue_maxsize=100, tier2_queue_alert_depth=80)
    audit.start()
    ks = KillSwitch(audit)
    server = ControlServer(
        host="127.0.0.1", port=0, token=token, killswitch=ks, audit=audit
    )
    await server.start()
    host, port = server.bound_address
    try:
        yield SimpleNamespace(
            server=server, killswitch=ks, audit=audit, conn=conn, host=host, port=port
        )
    finally:
        await server.stop()
        await audit.aclose()
        conn.close()


async def http_request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    headers: list[str] | None = None,
    body: bytes = b"",
) -> SimpleNamespace:
    """Issue one real HTTP/1.1 request and read the full response (server closes)."""
    reader, writer = await asyncio.open_connection(host, port)
    lines = [f"{method} {path} HTTP/1.1", "Host: argus"]
    if headers:
        lines += headers
    if body:
        lines.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
    writer.write(raw)
    await writer.drain()
    data = await reader.read()  # to EOF: the server sends `Connection: close`
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    head, _, resp_body = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    code = int(status_line.split()[1])
    resp_headers = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        resp_headers[name.strip().lower()] = value.strip()
    return SimpleNamespace(
        code=code, headers=resp_headers, text=resp_body.decode("utf-8"), raw=data
    )


def _bearer(token: str) -> list[str]:
    return [f"Authorization: Bearer {token}"]


# -- the trip -----------------------------------------------------------------


async def test_valid_token_trips_and_records_source(tmp_path: Path):
    async with harness(tmp_path) as h:
        assert h.killswitch.engaged is False
        resp = await http_request(h.host, h.port, "POST", "/kill", headers=_bearer(TOKEN))

        assert resp.code == 200
        assert '"status": "tripped"' in resp.text
        assert h.killswitch.engaged is True

        # The trip is on the record, attributed to the HTTP endpoint (distinct from
        # an audit-failure or heartbeat trip).
        row = h.conn.execute(
            "SELECT category, payload FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert row["category"] == "kill_switch.trip"
        assert f'"source": "{TRIP_SOURCE}"' in row["payload"]
        h.audit.verify_chain()


async def test_response_returns_only_after_trip_is_recorded(tmp_path: Path):
    # The 200 must not precede the Tier 1 write. By the time the response is read,
    # the trip record is already durable in the DB.
    async with harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "POST", "/kill", headers=_bearer(TOKEN))
        assert resp.code == 200
        count = h.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE category = 'kill_switch.trip'"
        ).fetchone()[0]
        assert count == 1  # committed before the client got its 200


async def test_double_trip_is_idempotent(tmp_path: Path):
    async with harness(tmp_path) as h:
        first = await http_request(h.host, h.port, "POST", "/kill", headers=_bearer(TOKEN))
        second = await http_request(h.host, h.port, "POST", "/kill", headers=_bearer(TOKEN))

        assert first.code == 200 and '"tripped"' in first.text
        assert second.code == 200 and "already_tripped" in second.text
        # Exactly ONE trip record — the no-op did not write a second.
        count = h.conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE category = 'kill_switch.trip'"
        ).fetchone()[0]
        assert count == 1


async def test_trip_returns_500_when_audit_write_fails(tmp_path: Path, monkeypatch):
    # The switch must still engage (safety stands), but the endpoint must NOT claim
    # 200 when the durable trip record could not be written.
    import sqlite3

    def boom(_rec):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    async with harness(tmp_path) as h:
        monkeypatch.setattr(h.audit, "_insert", boom)
        resp = await http_request(h.host, h.port, "POST", "/kill", headers=_bearer(TOKEN))

        assert resp.code == 500
        assert "write_failed" in resp.text
        assert h.killswitch.engaged is True       # halted regardless
        assert h.audit.failed is not None          # log latched failed


# -- auth ---------------------------------------------------------------------


async def test_missing_token_is_401(tmp_path: Path):
    async with harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "POST", "/kill")
        assert resp.code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        assert h.killswitch.engaged is False


async def test_invalid_token_is_401(tmp_path: Path):
    async with harness(tmp_path) as h:
        resp = await http_request(
            h.host, h.port, "POST", "/kill", headers=_bearer("wrong-token")
        )
        assert resp.code == 401
        assert h.killswitch.engaged is False


@pytest.mark.parametrize(
    "auth",
    [
        "Authorization: Token " + TOKEN,   # wrong scheme
        "Authorization: Bearer",           # no token
        "Authorization: Bearer" + TOKEN,   # no space (not the Bearer prefix)
        "Authorization: " + TOKEN,         # bare token, no scheme
    ],
)
async def test_malformed_auth_header_is_401(tmp_path: Path, auth: str):
    async with harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "POST", "/kill", headers=[auth])
        assert resp.code == 401
        assert h.killswitch.engaged is False


async def test_non_ascii_auth_header_is_401_not_500(tmp_path: Path):
    # A junk byte in the Authorization header must stay a clean 401 — the token is
    # compared as bytes precisely so hmac.compare_digest can't raise on non-ASCII.
    async with harness(tmp_path) as h:
        reader, writer = await asyncio.open_connection(h.host, h.port)
        writer.write(
            b"POST /kill HTTP/1.1\r\nHost: argus\r\n"
            b"Authorization: Bearer \xff\xfe\r\n\r\n"
        )
        await writer.drain()
        data = await reader.read()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        assert b"401" in data.split(b"\r\n", 1)[0]
        assert h.killswitch.engaged is False


async def test_auth_failure_logs_ip_and_records_but_never_the_token(
    tmp_path: Path, caplog
):
    async with harness(tmp_path) as h:
        with caplog.at_level(logging.WARNING, logger="argus.control"):
            resp = await http_request(
                h.host, h.port, "POST", "/kill", headers=_bearer("bad-guess")
            )
        assert resp.code == 401

        # The FACT of the failed attempt is visible (source IP), but the offered
        # token value is nowhere — not the configured token, not the guess.
        assert "127.0.0.1" in caplog.text
        assert TOKEN not in caplog.text
        assert "bad-guess" not in caplog.text
        assert TOKEN not in resp.raw.decode("latin-1")

        # A Tier 2 record marks the probe; it carries the peer, never a token.
        rows = await _wait_for_rows(h.conn, "control.auth_rejected")
        assert rows and all(TOKEN not in p and "bad-guess" not in p for p in rows)


# -- routing ------------------------------------------------------------------


async def test_unknown_path_is_404(tmp_path: Path):
    async with harness(tmp_path) as h:
        resp = await http_request(
            h.host, h.port, "POST", "/status", headers=_bearer(TOKEN)
        )
        assert resp.code == 404
        assert h.killswitch.engaged is False


async def test_wrong_method_on_kill_is_405(tmp_path: Path):
    async with harness(tmp_path) as h:
        resp = await http_request(h.host, h.port, "GET", "/kill", headers=_bearer(TOKEN))
        assert resp.code == 405
        assert resp.headers.get("allow") == "POST"
        assert h.killswitch.engaged is False


async def test_post_with_body_is_drained_and_still_trips(tmp_path: Path):
    # A well-formed POST may carry a body; the reader must drain it (so the client
    # can read the response) and the trip must still fire.
    async with harness(tmp_path) as h:
        resp = await http_request(
            h.host, h.port, "POST", "/kill",
            headers=_bearer(TOKEN), body=b'{"note":"pull it"}',
        )
        assert resp.code == 200
        assert h.killswitch.engaged is True


# -- bind / token loading -----------------------------------------------------


async def test_client_reset_mid_request_does_not_take_down_the_listener(
    tmp_path: Path, caplog
):
    # A client that connects and RSTs partway through a header must be a benign,
    # non-error close: the listener keeps serving, no task/socket leaks, and it is
    # NOT logged at ERROR (a probe or flaky phone must not spam the log).
    import socket
    import struct

    async with harness(tmp_path) as h:
        with caplog.at_level(logging.ERROR, logger="argus.control"):
            s = socket.create_connection((h.host, h.port))
            # SO_LINGER (0) → close() sends a RST rather than a clean FIN.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.sendall(b"POST /kill HTTP/1.1\r\nAuthorization: Bear")  # cut off mid-header
            await asyncio.sleep(0.05)
            s.close()
            await asyncio.sleep(0.05)

            # Listener survived: a normal request still trips.
            resp = await http_request(
                h.host, h.port, "POST", "/kill", headers=_bearer(TOKEN)
            )
        assert resp.code == 200
        assert h.killswitch.engaged is True
        # The reset was handled as benign, not an ERROR-with-traceback.
        assert "handler error" not in caplog.text


async def test_malformed_request_line_is_400(tmp_path: Path):
    # A garbage request line must be a clean 400, not a hang or a crash.
    async with harness(tmp_path) as h:
        reader, writer = await asyncio.open_connection(h.host, h.port)
        writer.write(b"this is not http\r\n\r\n")
        await writer.drain()
        data = await reader.read()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        assert b"400" in data.split(b"\r\n", 1)[0]
        assert h.killswitch.engaged is False


async def test_bind_failure_refuses_to_start(tmp_path: Path):
    # 192.0.2.1 (TEST-NET-1) is not an address on this host, so the bind fails —
    # standing in for a down Tailscale interface. It must fail loud, never fall
    # back to a wider bind.
    conn = connect(tmp_path / "argus.db")
    apply_migrations(conn)
    audit = AuditLog(conn, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    server = ControlServer(
        host="192.0.2.1", port=0, token=TOKEN, killswitch=KillSwitch(audit), audit=audit
    )
    try:
        with pytest.raises(ControlServerBindError, match="Tailscale"):
            await server.start()
    finally:
        await server.stop()
        await audit.aclose()
        conn.close()


def test_load_kill_token_present():
    assert load_kill_token({KILL_TOKEN_VAR: "abc"}) == "abc"


@pytest.mark.parametrize("env", [{}, {KILL_TOKEN_VAR: ""}])
def test_load_kill_token_missing_or_empty_raises(env):
    with pytest.raises(MissingKillTokenError, match=KILL_TOKEN_VAR):
        load_kill_token(env)


async def _wait_for_rows(conn, category: str, *, timeout: float = 2.0) -> list[str]:
    """Poll until the Tier 2 consumer has written a row of `category` (payloads)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = conn.execute(
            "SELECT payload FROM audit_log WHERE category = ?", (category,)
        ).fetchall()
        if rows:
            return [r["payload"] for r in rows]
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {category!r} rows within {timeout}s")
