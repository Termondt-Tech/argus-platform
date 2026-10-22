"""Authenticated HTTP control surface — the phone-reachable kill switch (spec §6).

Spec §6's first non-negotiable is a kill switch "reachable in one action, from a
phone, at any time." The in-process primitive lives in `risk/killswitch.py`; this
module is the always-on network surface that lets a phone reach it. It has TWO
state-changing routes — `POST /kill` and, since 2026-09-01, `POST /resume`;
alongside them sit two READ-ONLY monitoring routes (spec §3/§5) on the same
listener and the same auth — `GET /status` (JSON) and `GET /` (a plain HTML view
of the same data). The status data is assembled in `status.py`; this module only
dispatches to it and renders the result. Nothing the read routes return can
change system state.

**`/resume` exists because a halt now survives a restart** (`risk/kill_state.py`).
Once the durable state is what governs, something has to be able to clear it, and
that something must be a human: rehydration only ever RESTORES a halt, and
`/resume` is the only path to `KillSwitch.disengage` in the entire codebase (an
AST test pins it). It is the mirror of `/kill` in every respect but one — it
requires `?confirm=yes`, because it is the only route in the system that ENABLES
risk and a mis-tapped shortcut must not be able to reach it.

Design decisions, all load-bearing:

- **stdlib only, no HTTP framework.** The runtime is stdlib-only apart from the
  unavoidable tastytrade SDK, and this is one tiny, fully-controlled route on a
  private interface. Rather than pull an async HTTP framework (~8 transitive,
  C-extension packages) into the hash-pinned image for it, the server is a
  minimal reader on `asyncio.start_server`. The usual hand-rolled-HTTP hazards do
  not apply here: there is no keep-alive state machine (every response sends
  `Connection: close` and the socket is closed after), no chunked/pipelining
  support, and request smuggling needs a fronting proxy there is none of. Line
  and header counts are bounded, and the body is drained only up to a small cap.

- **Runs on the event loop, never blocks it (spec §4).** The handler awaits the
  same `KillSwitch.engage` the rest of the system uses, whose Tier 1 audit write
  is already routed off-loop via `asyncio.to_thread`. No synchronous I/O here.

- **Bearer auth, constant-time, token never logged.** The token comes from the
  environment (`ARGUS_KILL_TOKEN`), never config or code — same discipline as the
  broker credentials. It is compared with `hmac.compare_digest` and never appears
  in a log line or an audit record, on success or failure. A *failed* attempt is
  logged (source IP + timestamp) so a probe is visible, but only the fact, never
  the offered token.

- **200 means durably recorded.** A successful trip does not return 200 until the
  Tier 1 `kill_switch.trip` write has actually completed — the whole point of
  hitting this from a phone is knowing the halt is on the record, not merely
  accepted. If that write fails, the switch still latches engaged (safety stands)
  but the endpoint returns 500, because the durability the caller is relying on
  did not happen.

- **Idempotent.** Hitting `/kill` when already tripped is a safe no-op: 200,
  `already_tripped`, no second trip record. `/resume` mirrors it: 200,
  `not_engaged`, and no resume record — a resume row's `seq` relative to the
  newest trip's IS the durable answer, so one must never be written
  speculatively.

- **No environment gating.** Identical behaviour in shadow / sandbox / live — it
  is proven trustworthy in shadow precisely so it is ready when it matters.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from argus.alerts import AlertFn, log_alert
from argus.audit.log import AuditLog, AuditWriteError
from argus.risk.killswitch import KillSwitch

if TYPE_CHECKING:
    from argus.status import StatusService

logger = logging.getLogger("argus.control")

# Env var carrying the bearer token. A secret, so it lives in the environment
# (.env / systemd EnvironmentFile / secret store), NEVER in argus.toml or code —
# same convention as the tastytrade credentials.
KILL_TOKEN_VAR = "ARGUS_KILL_TOKEN"

# Routes. POST /kill and POST /resume are the state-changing ones; the GET routes
# are the read-only monitoring surface (spec §3/§5).
KILL_PATH = "/kill"
RESUME_PATH = "/resume"
STATUS_PATH = "/status"
ROOT_PATH = "/"

# Audit provenance for a trip that came through this endpoint. Distinct from
# "audit_log" (a failed audit write tripped it) and "heartbeat" (a sustained
# stall) so post-hoc review can tell a deliberate phone kill from an automated
# one. `daily_loss.breach` is a separate category entirely (the breaker).
TRIP_SOURCE = "http_endpoint"
TRIP_REASON = "kill switch tripped via HTTP control endpoint"

# The same provenance for the clear. One source string for both directions is
# deliberate: "a human, over the authenticated control surface" is the same fact
# either way, and the record's CATEGORY already says which direction it was.
RESUME_SOURCE = "http_endpoint"

# `/resume` is the only route that ENABLES risk, so it is deliberately not
# one-tap: a mis-fired shortcut or a stray POST must not be able to resume
# trading on a system somebody stopped on purpose. A query parameter is the
# cheapest confirmation that costs no body parsing on a hand-rolled reader, and
# it makes the resume URL structurally different from the kill URL.
RESUME_CONFIRM_PARAM = "confirm"
RESUME_CONFIRM_VALUE = "yes"
#: Optional free-text `reason=` on the resume, recorded when supplied. Bounded
#: because it is operator input that lands in a durable record.
RESUME_REASON_PARAM = "reason"
_MAX_RESUME_REASON = 200

# Conservative caps for the minimal reader. The client is a curl one-liner over
# Tailscale; these exist to bound a malformed or hostile request, not to support
# large payloads.
_LINE_LIMIT = 8192          # max bytes for the request line and each header line
_MAX_HEADER_LINES = 100
_MAX_BODY_BYTES = 64 * 1024  # /kill needs no body; we only drain to free the client
_READ_TIMEOUT_S = 10.0       # a client that opens a socket and stalls must not pin a task


class MissingKillTokenError(RuntimeError):
    """Raised when the kill-switch bearer token is absent from the environment.

    Fail closed: an unauthenticated kill endpoint is a broken safety rail, so the
    supervisor refuses to start without the token rather than exposing an open or
    disabled halt control.
    """


def load_kill_token(env: Mapping[str, str] | None = None) -> str:
    """Read the bearer token from the environment, or fail loud.

    Reports only the missing variable NAME, never a value (there is none to leak
    when it is missing, but the discipline matches `TastytradeCredentials`).
    """
    source = os.environ if env is None else env
    token = source.get(KILL_TOKEN_VAR)
    if not token:
        raise MissingKillTokenError(
            f"missing required env var {KILL_TOKEN_VAR}: the kill-switch control "
            f"endpoint has no bearer token and would be unauthenticated — refusing "
            f"to start"
        )
    return token


class ControlServerBindError(RuntimeError):
    """The control server could not bind its configured address.

    Almost always means the configured Tailscale interface is not present on the
    host (Tailscale down, wrong IP). Fails the whole startup loud — the endpoint
    must never silently fall back to a more exposed bind.
    """


class ControlServer:
    """Always-on `POST /kill` server, owned by the Application supervisor."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        killswitch: KillSwitch,
        audit: AuditLog,
        alert: AlertFn | None = None,
        status_service: "StatusService | None" = None,
        halt_state: Callable[[], Any | None] = lambda: None,
    ) -> None:
        self._host = host
        self._port = port
        # Compared as BYTES: hmac.compare_digest raises TypeError on a non-ASCII
        # str, and a client controls the header bytes — comparing bytes keeps a
        # junk byte a plain 401, not a 500.
        self._token_bytes = token.encode("utf-8")
        self._killswitch = killswitch
        self._audit = audit
        self._alert = alert or log_alert
        # Assembles the read-only status snapshot. Optional so the safety-critical
        # server still stands up (and unit-tests) with only the kill route; the
        # supervisor always wires it. When absent, the GET routes report 404.
        self._status_service = status_service
        # Late-bound `Application._halted`, read only by `/resume` and only to
        # tell the caller whether THIS process can act on the clear. Same seam and
        # same default as `StatusService.halt_state`, for the same reason: the
        # halt happens long after this server is built. Deliberately does not
        # change what `/resume` DOES — the durable state is cleared either way.
        self._halt_state = halt_state
        self._server: asyncio.Server | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind and begin serving. Raises `ControlServerBindError` on bind failure.

        The bind is the loud failure spec §6 requires: `asyncio.start_server`
        binds exactly `host:port` or raises — it never falls back to a wider
        address — so a missing Tailscale interface stops the process here.
        """
        try:
            self._server = await asyncio.start_server(
                self._handle, host=self._host, port=self._port, limit=_LINE_LIMIT
            )
        except OSError as exc:
            raise ControlServerBindError(
                f"could not bind the kill-switch control server to "
                f"{self._host}:{self._port} ({exc}); is the Tailscale interface up "
                f"and is server.bind_host this host's Tailscale IP?"
            ) from exc
        host, port = self.bound_address or (self._host, self._port)
        logger.info("kill-switch control server listening on %s:%s (POST /kill)", host, port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    @property
    def bound_address(self) -> tuple[str, int] | None:
        """The actually-bound (host, port). With ``bind_port = 0`` the OS assigns
        an ephemeral port; this is how a test discovers it."""
        if self._server is None or not self._server.sockets:
            return None
        sockname = self._server.sockets[0].getsockname()
        return str(sockname[0]), int(sockname[1])

    # -- connection handling ----------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if isinstance(peer, tuple) and peer else "unknown"
        try:
            async with asyncio.timeout(_READ_TIMEOUT_S):
                await self._serve(reader, writer, peer_ip)
        except (asyncio.TimeoutError, _BadRequest) as exc:
            # Malformed or slow request: answer 400 if we still can, then close.
            with contextlib.suppress(Exception):
                await self._respond(writer, 400, "Bad Request", {"error": str(exc) or "bad request"})
        except (ConnectionError, asyncio.IncompleteReadError):
            # The client vanished mid-request (reset the connection, or closed it
            # partway through a read or our response write). Benign — there is
            # nothing left to send and nothing to clean up beyond the socket, which
            # the `finally` closes. NOT an error, so no traceback: a probe or a
            # flaky phone connection must not spam ERROR-level logs, and it never
            # touches the listener, which serves the next connection on its own task.
            logger.debug("control client from %s disconnected mid-request", peer_ip)
        except Exception:
            logger.exception("control connection handler error")
            with contextlib.suppress(Exception):
                await self._respond(writer, 500, "Internal Server Error", {"error": "internal error"})
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer_ip: str
    ) -> None:
        method, path, headers = await self._read_request(reader)

        # Auth first, for EVERY request. A bad/absent token gets 401 and reveals
        # nothing about which routes exist. Constant-time compare; the token is
        # never logged, on success or failure.
        if not self._authorized(headers.get("authorization", "")):
            logger.warning(
                "control endpoint AUTH FAILURE from %s (%s %s)", peer_ip, method, path
            )
            # Non-safety-critical, so Tier 2. Records the fact of a probe (IP,
            # route) for the trail — never the offered token.
            self._audit.telemetry(
                "control.auth_rejected", {"peer": peer_ip, "method": method, "path": path}
            )
            await self._respond(
                writer, 401, "Unauthorized", {"error": "unauthorized"},
                extra_headers=("WWW-Authenticate: Bearer",),
            )
            return

        # Read-only monitoring routes (spec §3/§5): same listener, same auth as
        # /kill. Matched as exact (GET, path) pairs so everything else falls
        # through to the /kill handling unchanged — a wrong method on /kill still
        # 405s, and any other path (including POST /status) still 404s.
        if method == "GET" and path == STATUS_PATH:
            await self._serve_status(writer, as_html=False)
            return
        if method == "GET" and path == ROOT_PATH:
            await self._serve_status(writer, as_html=True)
            return

        # `/resume` is the ONLY route that carries a query string, and the split
        # happens here rather than at the top of this function on purpose: the
        # `/kill` comparison below still tests the FULL path, byte for byte, so
        # `/kill?anything` remains a 404 exactly as it always has. Widening the
        # safety-critical route as a side effect of adding this one would be a
        # silent change to the surface spec §6 names.
        if _route_of(path) == RESUME_PATH:
            if method != "POST":
                await self._respond(
                    writer, 405, "Method Not Allowed", {"error": "method not allowed"},
                    extra_headers=("Allow: POST",),
                )
                return
            await self._resume(writer, peer_ip, _query_of(path))
            return

        if path != KILL_PATH:
            await self._respond(writer, 404, "Not Found", {"error": "not found"})
            return
        if method != "POST":
            await self._respond(
                writer, 405, "Method Not Allowed", {"error": "method not allowed"},
                extra_headers=("Allow: POST",),
            )
            return

        await self._kill(writer, peer_ip)

    async def _kill(self, writer: asyncio.StreamWriter, peer_ip: str) -> None:
        """Trip the kill switch (once), await its Tier 1 write, then respond.

        Order matters: `engage` flips the switch to halted synchronously and only
        then awaits the audit write, so by the time it returns the trip is durably
        recorded (or the log has latched failed). We therefore do not return 200
        until that write has completed — that is the guarantee a phone caller is
        relying on.
        """
        was_engaged = self._killswitch.engaged
        if was_engaged:
            # Idempotent no-op: already halted (by this endpoint, the breaker, a
            # stall, or an audit failure). No second trip record.
            logger.info("kill endpoint hit from %s but already tripped", peer_ip)
            await self._respond(
                writer, 200, "OK",
                {"status": "already_tripped", "reason": self._killswitch.reason},
            )
            return

        logger.warning("KILL SWITCH TRIP requested from %s via HTTP endpoint", peer_ip)
        await self._killswitch.engage(TRIP_REASON, source=TRIP_SOURCE)

        if self._audit.failed is not None:
            # The switch is engaged (safety stands), but the durable trip record
            # did not persist — do NOT claim success. 500 tells the caller the
            # halt is on but not on the record, which is the honest state.
            self._alert("kill_trip_unrecorded", {"peer": peer_ip})
            await self._respond(
                writer, 500, "Internal Server Error",
                {"status": "tripped", "audit": "write_failed"},
            )
            return

        await self._respond(writer, 200, "OK", {"status": "tripped"})

    async def _resume(
        self, writer: asyncio.StreamWriter, peer_ip: str, query: str
    ) -> None:
        """Clear the halt (once), await its Tier 1 write, then respond.

        The mirror of `_kill`, and the same "200 means durably recorded"
        discipline — here it is doubly load-bearing, because the write is what
        PERMITS the flip rather than merely evidencing it (`KillSwitch.disengage`
        records first and flips only on success). So a 500 from this route means
        the halt is still in force, not that it might be.

        **`restart_required` is a report, never a condition.** A process holding a
        `HaltRecord` will not trade again whatever this route does — the latch is
        one-way by construction, and under `HaltGrade.ENTRIES` the session's
        monitor-only decision was taken once, at session start. Refusing the
        request there was considered and rejected: clearing the DURABLE state is
        the whole point of the route, and refusing would force an operator to SSH
        in order to reach something a phone should reach. So the clear always
        happens and the response tells the truth about who can act on it —
        which makes the runbook `assess → /resume → restart`, one restart.
        """
        params = parse_qs(query, keep_blank_values=True)
        # EXACTLY one `confirm`, and it must be the exact value. A repeated
        # parameter (`?confirm=no&confirm=yes`) is ambiguous, and resolving an
        # ambiguity in favour of resuming trading is the wrong default for the one
        # route that enables risk.
        if params.get(RESUME_CONFIRM_PARAM) != [RESUME_CONFIRM_VALUE]:
            logger.warning(
                "resume endpoint hit from %s WITHOUT confirmation — refused", peer_ip
            )
            await self._respond(
                writer, 400, "Bad Request",
                {"error": f"resume requires ?{RESUME_CONFIRM_PARAM}="
                          f"{RESUME_CONFIRM_VALUE}"},
            )
            return

        if not self._killswitch.engaged:
            # Idempotent no-op, mirroring `/kill`'s `already_tripped`. NO resume
            # record: `kill_state` decides engagement by comparing the newest
            # resume's `seq` against the newest trip's, and a speculative row
            # would sit in front of a FUTURE trip's restoration.
            logger.info("resume endpoint hit from %s but nothing is engaged", peer_ip)
            await self._respond(writer, 200, "OK", {"status": "not_engaged"})
            return

        halt = self._halt_state()
        restart_required = halt is not None
        # `str()`/`getattr` for the same reason `status.py` uses them: this module
        # deliberately does not import `app`, so the halt object is `Any` and a
        # test double predating the field reads as None instead of raising.
        halt_grade = str(getattr(halt, "grade", None)) if halt is not None else None
        detail: dict[str, Any] = {
            # The peer IP, exactly as `control.auth_rejected` records it — never
            # the token, on any route, on success or failure.
            "peer": peer_ip,
            "restart_required": restart_required,
            "halt_grade": halt_grade,
        }
        operator_reason = params.get(RESUME_REASON_PARAM, [""])[0][:_MAX_RESUME_REASON]
        if operator_reason:
            detail["operator_reason"] = operator_reason

        logger.warning("KILL SWITCH RESUME requested from %s via HTTP endpoint", peer_ip)
        try:
            await self._killswitch.disengage(source=RESUME_SOURCE, detail=detail)
        except AuditWriteError:
            # The clear did NOT happen: `disengage` records before it flips, so a
            # failed write leaves the switch engaged and the durable state intact.
            # Reporting anything but a failure here would tell an operator trading
            # had resumed when it has not.
            logger.exception("resume refused: the clear could not be recorded")
            self._alert("kill_resume_unrecorded", {"peer": peer_ip})
            await self._respond(
                writer, 500, "Internal Server Error",
                {"status": "engaged", "audit": "write_failed"},
            )
            return

        await self._respond(
            writer, 200, "OK",
            {
                "status": "resumed",
                "restart_required": restart_required,
                "halt_grade": halt_grade,
            },
        )

    # -- status (read-only) ------------------------------------------------

    async def _serve_status(self, writer: asyncio.StreamWriter, *, as_html: bool) -> None:
        """Assemble the read-only status snapshot and render it (JSON or HTML).

        Both renderings come from ONE assembled `StatusSnapshot`, so the JSON and
        HTML views can never drift. Read-only: nothing here mutates state. The DB
        reads happen off the event loop inside `assemble` (spec §4). The bearer
        token never appears in a snapshot — no field carries it, and it is never
        written to the audit log the tail comes from.
        """
        if self._status_service is None:
            await self._respond(writer, 404, "Not Found", {"error": "not found"})
            return
        snapshot = await self._status_service.assemble()
        if as_html:
            await self._respond_html(writer, 200, "OK", snapshot.to_html())
        else:
            await self._respond(writer, 200, "OK", snapshot.to_json_dict())

    # -- auth --------------------------------------------------------------

    def _authorized(self, auth_header: str) -> bool:
        prefix = "Bearer "
        # The scheme check is not constant-time, but it only reveals the presence
        # of the "Bearer " prefix, not the secret. The TOKEN comparison itself is
        # constant-time, which is what matters. `auth_header` was decoded latin-1
        # (a 1:1 byte map), so encoding it back to latin-1 recovers the exact
        # header bytes to compare against the token's UTF-8 bytes.
        provided = auth_header[len(prefix):] if auth_header.startswith(prefix) else ""
        return hmac.compare_digest(provided.encode("latin-1"), self._token_bytes)

    # -- minimal HTTP/1.1 read/write --------------------------------------

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str]]:
        """Parse the request line + headers; drain any (bounded) body.

        Returns `(method, path, headers)` — headers lowercased, and kept LOCAL
        (never on `self`, which is shared across concurrently-handled
        connections). Raises `_BadRequest` on anything malformed. Deliberately
        supports only what a curl `POST` sends: a request line, headers, and an
        optional small body announced by `Content-Length`.
        """
        request_line = await self._read_line(reader)
        parts = request_line.split()
        if len(parts) != 3:
            raise _BadRequest("malformed request line")
        method, path, _version = parts

        headers: dict[str, str] = {}
        for _ in range(_MAX_HEADER_LINES):
            line = await self._read_line(reader)
            if line == "":
                break
            name, sep, value = line.partition(":")
            if not sep:
                raise _BadRequest("malformed header line")
            headers[name.strip().lower()] = value.strip()
        else:
            raise _BadRequest("too many header lines")

        # Drain a declared body so the client can read our response cleanly. We do
        # not use it — auth is in the header — but a well-formed POST may carry one.
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                n = int(content_length)
            except ValueError as exc:
                raise _BadRequest("invalid content-length") from exc
            if n < 0 or n > _MAX_BODY_BYTES:
                raise _BadRequest("content-length out of range")
            if n:
                with contextlib.suppress(asyncio.IncompleteReadError):
                    await reader.readexactly(n)

        return method, path, headers

    async def _read_line(self, reader: asyncio.StreamReader) -> str:
        """Read one CRLF-terminated line, decoded, without the terminator.

        `readuntil` enforces the stream's byte `limit`, so an over-long line
        (a client that never sends a newline) raises rather than buffering without
        bound; we surface that as a bad request.
        """
        try:
            raw = await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            # EOF before a newline — treat whatever we got as the (final) line.
            raw = exc.partial
        except (asyncio.LimitOverrunError, ValueError) as exc:
            raise _BadRequest("line too long") from exc
        return raw.decode("latin-1").rstrip("\r\n")

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        reason: str,
        body_obj: dict[str, Any],
        *,
        extra_headers: tuple[str, ...] = (),
    ) -> None:
        await self._write_response(
            writer, code, reason, "application/json",
            json.dumps(body_obj).encode("utf-8"), extra_headers,
        )

    async def _respond_html(
        self, writer: asyncio.StreamWriter, code: int, reason: str, html_body: str
    ) -> None:
        await self._write_response(
            writer, code, reason, "text/html; charset=utf-8",
            html_body.encode("utf-8"), (),
        )

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        reason: str,
        content_type: str,
        body: bytes,
        extra_headers: tuple[str, ...] = (),
    ) -> None:
        # Every response is `Connection: close` (no keep-alive state machine) and
        # carries an explicit Content-Length — same discipline for JSON and HTML.
        lines = [
            f"HTTP/1.1 {code} {reason}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
            *extra_headers,
            "",
            "",
        ]
        writer.write("\r\n".join(lines).encode("ascii") + body)
        await writer.drain()


def _route_of(path: str) -> str:
    """The path without its query string.

    Used ONLY to match `/resume`. `/kill`, `/status` and `/` are still matched
    against the raw path, so this cannot widen them.
    """
    return path.partition("?")[0]


def _query_of(path: str) -> str:
    return path.partition("?")[2]


class _BadRequest(Exception):
    """Internal: a malformed request the handler answers with 400."""
