# Monitoring surface — read-only status (spec §3/§5)

*The read-only `GET /status` + `GET /` surface on the control server (spec 3/5).*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


The same control server (`control.py`, same Tailscale bind, same `ARGUS_KILL_TOKEN`
bearer auth) hosts two **read-only** routes for glancing at the always-on process.
This is deliberately "live status + kill switch, not a screener" (spec §5) — a
current snapshot plus a recent-audit tail, NOT an AG-Grid candidate browser. No new
config, bind, or secret; purely additive to the dispatcher (the `POST /kill` path
is untouched). Anything that isn't `POST /kill`, `GET /status`, or `GET /` is a
clean 404; a missing/bad token is 401 exactly as for `/kill`.

- `GET /status` — JSON, machine-readable.
- `GET /` — the same data rendered as dependency-free HTML (no external JS/CSS/
  fonts), legible on a phone; a manual reload is the refresh model.

Both come from ONE assembly (`status.py`: `StatusService.assemble` builds a
`StatusSnapshot`; `to_json_dict()` / `to_html()` are the two renderings, so they
can't drift). Fields: `environment` + process up-since/uptime; scheduler/session
phase (idle / logging_in / reconciling / running / ended / aborted) + next session
open; **market-data and account staleness AGES** (the actual age plus the
threshold and the fail-closed `is_stale` verdict, so drift is visible before it
trips); event-loop heartbeat lag; Tier-2 audit queue depth (spec §6 alertable);
kill-switch state (+ source/timestamp from the trip record) and daily-loss breaker
state (derived from today's `daily_loss.breach` record); today's **RAW**
`shadow.would_submit` count (explicitly NOT the settlement-aware deduped
trade-idea count `tools.mark` computes — do not reimplement that here); **LINK
HEALTH** for both streams (`LinkHealth`, added 2026-08-05 — connected/not,
session-scoped reconnect counts, and the account REST refresh's CONSECUTIVE
failure count); and the last ~15 audit records, most recent first.

**Why link health had to be here.** `/status` could say how OLD the data was and
not one thing about the links carrying it, so "is my account link healthy?" was
answerable only from the session's journal `{...}` block — an all-time ledger that
got misread as an active fault three times in one day (see "The Slack alert
storm"). An operator on a phone cannot difference two journal lines. Three rules
this follows, all learned from that misread:
  - **Counts are SESSION-scoped, not career totals**, because the adapters are
    rebuilt each trading day. Deliberately the opposite of the journal block.
  - **Everything is DERIVED live off the adapters** (`DXLinkMarketData.connected`/
    `.reconnects`, `AlertStreamerAccount.connected`/`.reconnects`/
    `.consecutive_refresh_failures`) through a CALLABLE, exactly like `risk_source`.
    Nothing new is measured and nothing is cached, so it cannot report the state at
    09:40 forever.
  - **Colour follows what is ACTIONABLE, not what is nonzero.** A reconnect COUNT
    is the recovery logic working and stays muted and labelled `(recovered)`;
    flagging it would rebuild the exact false positive this row exists to remove.
    Red is for `connected == False` right now, or a NONZERO consecutive-refresh
    failure count — which is the leading indicator for `account_data_age_s`
    climbing toward `account_stream_max_age_s` and blocking entries.

How the plumbing stays honest:
- **Session-scoped state reaches the process-lifetime server via `SessionStatus`**
  (`status.py`) — a tiny publisher the session body writes its phase and live
  `RiskSnapshotBuilder.build` handle into, and clears on session end (so ages read
  "no active session" between days). Wired into the real body with
  `functools.partial`, so no session/test signatures changed.
  - **What clears on session end is HANDLES, not history** (corrected 2026-08-19).
    The rule above is about live sources: reading one after teardown reports an age
    off a dead feed. `session_counts`'s baseline was swept into the same clear when
    it was added in v0.3.4, and it does not belong there — it is a static integer
    snapshot, and the totals it subtracts from live in the audit DB, which outlives
    the session. The effect was that `/status` answered "what happened today?" all
    session and returned `{}` at the EOD read. It now survives until
    `session_starting` replaces it. **Anything added to `SessionStatus` from here
    needs that question asked of it: is this a handle, or is it a record?**
- **DB reads use the process's OWN writer connection** via `AuditLog.read(fn)` —
  serialized on the chain lock (the shared connection is never touched by two
  threads at once) and run off the loop with `asyncio.to_thread` (spec §4). NOT the
  external `file:...?mode=ro` pattern (that's for outside tools, not the writer
  process itself).
- **The bearer token never surfaces here** — no snapshot field carries it, and it
  is never written to the audit log, so it can't appear in the recent-record tail.

**Viewing status from a phone** reuses the same mechanism as the kill button: an
iOS Shortcut (or Android Tasker task) wrapping a "Get Contents of URL" action, GET
method, one header `Authorization: Bearer <token>`, URL
`http://<tailscale-ip>:<port>/status` (or `/` for the HTML view — some HTTP-client
Shortcuts render the returned HTML). Same token, same tailnet, same
`[server]` host/port as the kill button; pin a second "Argus status" icon next to
the kill icon. From a terminal on the tailnet:

```
curl -fsS -H "Authorization: Bearer $ARGUS_KILL_TOKEN" http://<tailscale-ip>:<port>/status
```

Unlike `/kill`, these are safe to hit anytime — they change nothing.

## Why this is a hand-rolled server and not a framework

*Relocated from `CLAUDE.md`'s Build/test section, where the reasoning sat
apart from the component it justifies. Verbatim.*

Python 3.12, `src/` layout. The runtime is **stdlib-only apart from the
tastytrade SDK** (sqlite3, tomllib, asyncio) — and deliberately stayed that way
for the HTTP control surface: `control.py`'s server is a minimal reader on
`asyncio.start_server`, not a web framework. That was a considered call (spec §6
Chunk 2). aiohttp was the obvious alternative, but the routes are a handful of
tiny, fully-controlled endpoints on a private interface, and the usual
hand-rolled-HTTP hazards do not apply: every response is `Connection: close` (no
keep-alive state machine), there is no chunked/pipelining support, request
smuggling needs a fronting proxy there is none of, and line/header counts and the
body are bounded. The Chunk 3 status routes (`GET /status`, `GET /`) stayed within
that envelope — read-only GETs with bounded, pre-built responses — so they did
NOT tip the balance toward a framework. Pulling ~8 transitive C-extension packages
into the `--require-hashes` image did not pay for itself against the deployment's
minimal-auditable-supply-chain goal. If a future chunk needs real HTTP surface
area (a richer monitoring API with many routes, content negotiation, etc.),
revisit — that is the point at which a framework earns its place.
