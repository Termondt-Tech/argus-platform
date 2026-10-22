> **This is the private operating README as it stood at the export, redacted.** Hosts,
> paths, addresses and the account are placeholders; sections about the owner's other
> strategy and the host's own plumbing are removed; links to documents that were not
> published say so. It is kept because it is the record of how the platform was run.

# Argus — Operator Guide


Fully automated 0DTE SPX credit-spread execution system.

> ## ⚠️ Current phase: the SPX book is KILLED and STOPPED
>
> - The kill switch was engaged on 2026-10-01.
> - The service was stopped and disabled on 2026-10-05.
> - v0.5.5 was never deployed.
> - Decommission (private record, not published) steps 1–3 are done, and step 4 is not yet run.
>
> **The droplet's profile is still `live`.** `/etc/argus/host.env` reads `ARGUS_ENV=live`, so it
> would load `argus.live.toml`, write `argus-live.db` and address production tastytrade account
> `5XX00000`.
> - **If anything restarted it,** it would boot with the kill switch engaged (rebuilt from the
>   audit log) and could not open a trade.
> - **The SPX commands in this guide still address the live account.**
> - **Read `host.env` and the loaded profile's `environment` key as the authority.** This line is
>   prose and can lag them.
>
> Argus traded live at minimum size from the cutover on 2026-09-15 at 16:30 ET until the kill.
> Phase 0 (shadow) and Phase 1 (cert live-fire) are complete and historical.
> → [live cutover runbook](live-cutover-runbook.md)

This is the day-to-day operator reference: how to configure signals, run a shadow
session, and read what comes out. For architecture and design rationale see
[`docs/0dte_spx_desk_spec.md`](0dte_spx_desk_spec.md) and `CLAUDE.md`.

> **Times are US/Eastern for trading logic, UTC in the audit log.** The entry
> window is ET. Audit timestamps are UTC — subtract for your local zone.

> ### ⚠️ `argus-live.db` and `argus.db` are BOTH production data
>
> **Since 2026-09-15 there are two.** `argus-live.db` is the LIVE audit log — real
> orders, real fills, real money — started empty at the cutover and **never reset**
> (the 24 h sandbox wipe keys on `environment == "sandbox"`, so every row in its
> `positions` table is a fact about a real account). `argus.db` holds the finished
> cert hash chain and the accumulating shadow/sandbox signal record, intact beside
> it. **Never point a test, experiment, benchmark, or debugging run at either.**
>
> - Real cert data-collection sessions → `argus.sandbox.toml`. Live trading →
>   `argus.live.toml`, which is the droplet's own profile and nothing else's.
> - **Everything else** → `argus.dev.toml`, which writes to the gitignored
>   `dev/` directory:
>   `--config argus.dev.toml`, or `ARGUS_ENV=dev`.
> - **There is no default config path.** A bare `python -m argus` and a bare
>   `argus.tools.*` both refuse — which is what retires the mechanism below
>   rather than leaving it to discipline.
> - Reading for analysis → always read-only:
>   `sqlite3 'file:argus-live.db?mode=ro'` (and the same for `argus.db`).
>
> `db_path` is resolved against your **working directory**, so a bare
> `python -m argus` from the repo root used to write straight into the live log. On
> 2026-07-21 exactly that happened alongside a running session: the stray process
> took three `seq` values, wedged the session's audit writer, and cost ~80 minutes
> of unrecorded signal data.
>
> **Since 2026-09-04 that command refuses to run.** There is no default config
> path: `python -m argus` needs `ARGUS_ENV` (the profile — `dev` loads
> `argus.dev.toml`) or an explicit `ARGUS_CONFIG`, and every `argus.tools.*` entry
> point now *requires* `--config`. The habit still matters, but it is no longer the
> only thing standing between you and the live database.

---

## Contents

1. [One-time setup](#1-one-time-setup)
2. [Configuring signals](#2-configuring-signals)
3. [Starting and stopping](#3-starting-and-stopping)
4. [Running in Docker (the droplet)](#4-running-in-docker-the-droplet)
5. [Reading the console output](#5-reading-the-console-output)
6. [Querying the audit log (SQL)](#6-querying-the-audit-log-sql)
7. [All commands](#7-all-commands)
8. [Live vs. sandbox data](#8-live-vs-sandbox-data)
9. [Safety behaviour you will actually see](#9-safety-behaviour-you-will-actually-see)
10. [Troubleshooting](#10-troubleshooting)
11. [Good to know](#11-good-to-know)

---

## 1. One-time setup

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
./.venv/bin/python -m pytest        # sanity check — should be all green
```

**Credentials** live in `.env` at the repo root (gitignored, never committed).
Copy `.env.example` and fill in. Every variable names both its **role** (can it
move money?) and its **world** (cert or production) —
`TASTYTRADE_<ROLE>_<WORLD>_<FIELD>`, role first so `grep TASTYTRADE_TRADING_` is a
complete audit of what can reach an order:

| Variable pair | Role / world | Required |
|---|---|---|
| `TASTYTRADE_TRADING_CERT_CLIENT_SECRET` / `..._REFRESH_TOKEN` | Trading + account, cert | **Always** — every non-live profile, and the market-data fallback |
| `TASTYTRADE_TRADING_LIVE_CLIENT_SECRET` / `..._REFRESH_TOKEN` | Trading + account, production | Only when the profile declares `environment = "live"` |
| `TASTYTRADE_MARKETDATA_LIVE_CLIENT_SECRET` / `..._REFRESH_TOKEN` | Read-only quotes/greeks, production | Whenever `[marketdata] source = "live"` — true in both shipped profiles |

These are Argus's **own** tastytrade app registrations. Cert and production are
separate worlds with separate logins — a cert token cannot read live quotes, and
vice versa.

**Both trading worlds live here at once, and the cutover does not touch this
file.** The profile (`/etc/argus/host.env`) selects between them, so going live —
and rolling back — is that one file changing. A `live` profile refuses to start if
its live slot is missing, or if that slot holds the cert values.
→ [environment-keyed trading credentials](decisions/2026-09-09-trading-credentials-are-environment-keyed.md)

Verify the shape of what's loaded (never prints secret values):

```bash
./.venv/bin/python -m argus.tools.credcheck
```

**One more required secret:** `ARGUS_KILL_TOKEN` — the bearer token for the
kill-switch endpoint (§9). `python -m argus` **refuses to start without it** (an
unauthenticated halt control is a broken safety rail), so set it in `.env` even in
shadow. It never goes in the config file or on a command line — only `.env`. Generate
a long random value:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### The three config files — one per PROFILE

| File | Profile | Writes to | Use for |
|---|---|---|---|
| `argus.sandbox.toml` | `sandbox` | `argus.db` — **the cert audit log** | Real cert/sandbox sessions whose data you intend to analyse. **No longer the deployed profile** since 2026-09-15. |
| `argus.live.toml` | `live` | `argus-live.db` | **Live trading, and the DEPLOYED profile since 2026-09-15.** Its own database, deliberately: live must never continue the cert hash chain. |
| `argus.dev.toml` | `dev` | `dev/argus-dev.db` (gitignored) | Everything else: smoke tests, tool runs, debugging, experiments. |

**The profile names the FILE; the file's own `environment` key still decides
trading behaviour**, and the loader refuses to start when the two disagree — the
assertion is armed by the filename, so `argus.<profile>.toml` is not a convention
you may quietly break.

```bash
ARGUS_ENV=dev python -m argus                  # profile -> argus.dev.toml
ARGUS_CONFIG=/tmp/scratch.toml python -m argus # explicit path, WINS over ARGUS_ENV
python -m argus                                # ConfigError. There is no default.
```

Every `argus.tools.*` entry point takes `--config` and **requires** it; the tools
do not read either variable. The three files are full standalone files, not a
base plus overlays, and they carry the same 83 keys —
`tests/test_config_profiles.py` asserts that, loads all three through the real
loader, and fails on any value divergence that is not on a reasoned allowlist.

---

## 2. Configuring signals

All tuning lives in the profile config —
[`argus.sandbox.toml`](../argus.sandbox.toml) for cert. **No restart-free reload**
(and none is planned) — edit the file, then start a new run. The knobs that decide whether a signal fires:

### `[signal]`

| Field | Meaning | Notes |
|---|---|---|
| `sides` | `["put"]`, `["call"]`, or both | A put credit spread is bullish, a call credit spread bearish. Both = evaluate each independently. |
| `target_short_delta` | Delta magnitude for the short strike (e.g. `0.15`) | Higher = closer to the money = more credit **and** more risk. |
| `short_delta_source` | `"mid"` or `"feed"` (REQUIRED, no default; `"mid"` in all three profiles since v0.4.6) | Which delta picks the short strike. `"mid"` uses the delta implied by each OTM leg's own mid, the same quotes the credit is computed from; `"feed"` is the greeks feed's delta (the pre-v0.4.6 behaviour). **`"feed"` is the rollback**: a config-only release, after the close, never with a position open. Never pool call outcomes across the change; split on `delta_source`. |
| `width` | Desired spread width in points | Long leg is this far OTM from the short. |
| `max_width` | Hard cap on **actual** strike distance (`5.0` live since v0.5.4, was `10.0`) | Ladders have gaps; the long leg is the nearest strike ≥ `width` out, up to this. Economics use the *actual* distance, not `width`. At `max_loss_per_position` 475 nothing wider than ~5.25 points can open, so `5.0` makes a gapped long read `no_long_leg_within_max_width` instead of proposing a spread the gate must refuse. |
| `min_credit` | Minimum net credit (points) to accept | **The usual binding constraint.** Reject if the spread pays less. |
| `entry_earliest` / `entry_latest` | ET time window, `"HH:MM"` | No entries outside it. |
| `cadence_s` | Seconds between evaluations | `5.0` is fine; every cycle emits one telemetry row. |

### `[risk]` — hard caps the gate enforces

**Every size rail is enforced twice, and the tightest one governs**: `size_contracts`
chooses a size against all three, and `PreTradeGate` re-checks the resulting proposal.
The two absolute rails are never gated on a readable NLV — an unreadable account
snapshot must not excuse an oversized position.

| Field | Live value | Meaning |
|---|---|---|
| `daily_max_loss` | `1050.0` | (v0.5.2, was `950.0`.) Dollar loss that trips the daily circuit breaker (halts new entries; it cannot close what is already open). |
| `max_concurrent_positions` | `2` | (v0.5.2, was `1`.) Concurrent **spread** cap — the gate reads `RiskSnapshot.open_spread_equivalent`, never a leg count, and that term also counts trade ideas with an opening order still working at the broker. |
| `position_size_pct` | `0.03` | Max fraction of NLV per position (relative rail). The gate rejects anything larger; sizing targets it. |
| `max_loss_per_position` | `475.0` | (v0.5.2, was `950.0`, so `2 × 475` gives the second slot one ordinary stop of slack under the day's `1050`.) Absolute dollar rail — max loss the proposal may carry at its actual strike distance. |
| `max_contracts_per_position` | `1` | (v0.5.2, was `2`.) Absolute contract rail. Two slots of one contract replace one slot of two: **no rail rose**, so this does not scale the live phase. |
| `side_balance` / `max_side_imbalance` | `true` / `1` | (v0.5.2, new.) At most one open spread per side — chosen in the orchestrator, refused by the gate (`side_imbalance_exceeded`); `false` is the pre-v0.5.2 entry path exactly, where a second spread could be a chained same-side pair (about 30% of days in the 23-session replay). See [v0.5.2](releases/v0.5.2.md) and [the research](../RESULTS.md). |

> ### ⚠️ `max_concurrent_positions` is welded to `daily_max_loss`
>
> The invariant to preserve is
> **`max_concurrent_positions × max_loss_per_position ≤ daily_max_loss`** — currently
> `2 × 475 = 950 ≤ 1050` (`1 × 950 = 950`, exactly, before v0.5.2). The breaker only stops NEW entries and cannot close what
> is open, so breaking that inequality lets it trip having already authorized more
> exposure than it caps. **Move one, re-check the other two.**
>
> These are the live-minimum-size numbers and they are enforced with real money. Cert
> was synced DOWN to them deliberately so the cutover would not also change what a
> trade costs — note cert never exercised the breaker half (day P&L is structurally
> `0` there), and live does.
> → [live-fire pass](decisions/2026-08-13-live-fire-sandbox-pass.md)

### `[watchdog]` — what closes a position

The signal knobs above decide what gets opened; these decide what gets closed, and
on a live profile they are the keys that bound a loss. Exits are a **virtual stop**
evaluated in-process against live quotes and handed to a price-walking loop — never
a stop order sent to the broker, never an unbounded market order.

| Field | Live value | Meaning |
|---|---|---|
| `credit_trigger_enabled` | `false` | (v0.5.2, new.) The strike-touch stop: the 2×-credit mark trigger below no longer fires, and the emergency exit is spot reaching the short strike alone. `true` is the config-only rollback to the credit stop. |
| `credit_multiple` | `3.0` | Inert while `credit_trigger_enabled = false` — it is the rollback value (was `2.0`, the live multiple, before v0.5.2). `credit_stop` is still computed and recorded (the profit target and the blind page share its input). |
| `underlying_buffer_points` | `0.0` | (v0.5.2, was `5.0`.) Now the primary loss driver under the strike-touch stop: fires when SPX reaches the short strike itself, not a buffer short of it. |
| `sustained_s` | `5.0` | Debounce. A trigger must hold this long before it fires, so one wide quote cannot exit a position. |
| `flatten_before_close_s` | `1800.0` | The third trigger, and the only one nothing can blind: an **offset** before *that day's* close, so 15:30 ET normally and 12:30 on a half-day. Same exit path, same guarded walk — it changes *when*, never *how*, and it will not invent a price for an unquotable spread. |
| `exit_walk_away_pct` / `exit_walk_min_steps` | `0.20` / `3` | How far the exit walk concedes before it crosses the market. The `min_steps` floor exists because a percentage of a near-zero price vanishes below half a tick — the flatten's modal regime. Raised 1 → 3 in v0.4.3. |
| `exit_unattempted_max_s` | `30.0` | Bound on **silence**: how long a triggered position may go with no order in the market before it pages, repeating until it clears. |
| `unmanaged_order_max_s` | `900.0` | P5's cancel recheck backs off to this while the broker still says an abandoned order is working. |
| `unmanaged_order_cancel` | `true` | Whether that recheck **sends** the re-cancel or only records `would_cancel`. Promoted to `true` in live in v0.4.5 (2026-09-20). |
| `flatten_deferral` | `"on"` | Whether a far-OTM 15:30 flatten may be **deferred** (2026-09-25): only when its insurance is at least `k` times overpriced — `k·P_touch(z)·W ≤ one tick`, `z` the volatility- and time-standardized distance to the underlying trigger's barrier — for a position nothing has been sent for, whose backstop can see and whose broker is answering. It only ever removes the flatten; the stops stay live to the close. `"record"` decides and records but always flattens; `"off"` is the permanent rollback. Refused `"on"` unless `max_concurrent_positions × max_loss_per_position ≤ daily_max_loss` — the welded invariant, which bounds what one barrier event can cost across every deferred position. |
| `flatten_defer_insurance_multiple` | `2.0` | (v0.5.4, was `10.0`; [decision](decisions/2026-09-28-flatten-deferral-k-2.md).) `k` — POLICY. Raising it is the safe direction and needs no analysis; lowering it needs a `tools.flatten_calibration` rerun and a decision record. |
| `flatten_defer_tail_z99` / `flatten_defer_tail_alpha` | `2.60` / `2.72` | (v0.5.5; `2.38` / `2.80` in v0.5.4, `2.33` / `2.93` before.) CALIBRATION OUTPUTS of `tools.flatten_calibration` — never hand-tuned; `alpha` refused outside `(1, 3]`. |

The full reasoning for each value — including why cert cannot tune most of them — is
inline in the profile's own `trigger_source` / `walk_source` prose, and in
[the second exit-ladder pass](decisions/2026-09-17-exit-ladder-second-pass.md).

### `[market]` — when the always-on supervisor runs (scheduling, not strategy)

`python -m argus` runs one session per **trading day** and idles otherwise. These
knobs decide when — pure scheduling correctness, distinct from macro-event
blackout days, which are **live** (since 2026-07-20), enforced separately in the
signal layer, and regenerated with `tools.blackout_dates` (§7).

| Field | Meaning |
|---|---|
| `session_open` / `session_close` | ET session window on a normal day (e.g. `"09:40"`–`"16:00"`). `session_open` sits a few minutes after the 09:30 open so the chain warms up before the entry window. |
| `half_day_close` | ET early close on a half-day (e.g. `"13:00"`). The session ends here — always the *earlier* close. |
| `holidays` | ISO dates (`YYYY-MM-DD`) of NYSE full closures — no session runs. |
| `half_days` | ISO dates that close early at `half_day_close`. A date in both lists is a full closure (holidays win). |
| `login_attempts` / `login_retry_interval_s` | Session-open login retry: how many attempts, how far apart. Kept few and slow (ban-aware) — see below. |

> **This is an operator-maintained calendar.** The market calendar can't be
> derived from code. Update `holidays`/`half_days` each year against the official
> NYSE calendar. A stale list would run a session on a market holiday; on the
> droplet, config is bind-mounted, so a `git pull` is enough (no rebuild).

> **`login_*` and ban risk.** Each failed OAuth refresh accrues toward
> tastytrade's undocumented IP-block threshold, so the default (6 attempts,
> 10 min apart ≈ 50 min) is deliberately few-and-slow — it rides out the cert's
> post-reset warmup without hammering the auth endpoint. If cert stays down past
> the window, the day is **missed** (acceptable in shadow), which is far cheaper
> than an 8-hour block that would also cost the next morning.

### `[broker] http_timeout_s` — the broker REST bound

Timeout (seconds) for every tastytrade REST call. This exists because the
alternative is an *inherited* default nobody chose: the SDK builds its httpx
client with no timeout, so calls silently used **httpx's 5-second default**
until 2026-07-23, when the droplet's first shadow run died on a cold
`GET /customers/me/accounts` at exactly that bound. Cert is slow on a first
authenticated call from a new host.

It is the client-wide *default*; callers needing a tighter bound still set their
own (the account liveness refresher wraps its round trip in
`asyncio.timeout(account_refresh_timeout_s)`, which cancels first and is
unaffected, and the reconcile fetches use `[reconcile] request_timeout_s` below).
`30.0` is a sane starting value.

### `[reconcile]` — how long the broker gets to answer, and for how long we keep asking

Added 2026-08-26, after a cert 502 on the process-start reconcile exited the
process **five times in 33 seconds** — Docker restarting into the same wall, with
`/status` and `/kill` down for every cycle and no halt reason recorded anywhere.
→ [broker unreachable](decisions/2026-08-26-reconcile-broker-unreachable.md)

| Key | Value | What it bounds |
|---|---|---|
| `request_timeout_s` | `10.0` | ONE HTTP attempt. Deliberately *not* `broker.http_timeout_s` — that 30s is sized for a cold FIRST authenticated call, and by the time the reconcile runs it is the second or third call of the process. Anchored at 2× the tuned `staleness.account_refresh_timeout_s`. Only bites when the broker accepts a socket and never answers; a 502 or a refused connection returns in milliseconds. |
| `retry_interval_s` | `60.0` | The OUTER loop's cadence. Inside one attempt there is already a 3-attempt burst with `min(2·n, 5)` backoff. |
| `retry_budget_s` | `1800.0` | Total, then give up. 30 min covers a cert outage of realistic length while a permanently broken deployment still reaches a loud terminal state the same morning. |

**While the outer loop runs the process is alive, bound, serving `/status` and
`/kill`, and NOT halted** — the scheduler has not started, so a success needs
nothing undone. Only the exhausted budget is terminal: at process start it PARKS
(`source=broker_unreachable`, one Tier 1 `reconcile.unreachable` and one alert); per
trading day it aborts the day and the process waits for tomorrow. A `POST /kill`
during the retry ends it immediately.

**A broker that ANSWERED and disagreed is untouched by all three** — that is
`reconcile.result` and it still halts with zero retry.

### `[audit] synchronous` — how hard the log is written

| Value | Behaviour | Use |
|---|---|---|
| `"NORMAL"` | fsync at checkpoint only. No corruption risk; a **host**-level crash (droplet reset, hypervisor failure) can roll back the last commits. A plain process crash is already safe. | Shadow — nothing submitted, nothing held. |
| `"FULL"` | fsync every commit; durable before `COMMIT` returns. | **Required for `environment = "live"`** — startup refuses otherwise. |

Measured on the Docker volume, single-record commits: NORMAL p50 0.009 ms / p99
0.024 ms; FULL p50 0.081 ms / p99 1.86 ms. At Argus's ~0.6 writes/sec, FULL costs
about 50 microseconds per second of runtime — the latency argument for NORMAL
doesn't survive contact with the real write rate.

It matters beyond forensics: the audit DB also holds the `positions` table that
reconciliation compares the broker against on restart. Losing the tail means
reconciling against expected state quietly missing its most recent entry — either
a false mismatch that blocks the open, or a "clean" reconcile against a position
the record has forgotten.

### Tuning `min_credit` from real data

`min_credit` is the highest-leverage knob and should be set **from the audit log,
not guessed** (see [§6](#6-querying-the-audit-log-sql)). Every no-signal
evaluation records the credit it *would* have collected and why it was rejected,
so after a session you can ask "what `min_credit` would have fired, and how often?"
directly.

The other half is **scoring what did fire** — run `argus.tools.mark` after the
close ([§6](#scoring-a-session-against-settlement)). Firing rate alone can't tell
you whether firing was *right*: a session that produced a candidate on 95% of
evaluations isn't a signal, it's "always on", and only the settlement outcome
distinguishes a good threshold from a loose one.

> **These are shadow-mode tuning knobs, not a validated edge.** The baseline
> strategy exists to generate candidates so you can *measure* quality. Real
> parameters come from your own shadow/backtest data.

---

## 3. Starting and stopping

### Two ways to run Argus

Everything Argus does is `python -m argus…`. There are two environments it can
run in, and every command in this guide works in both — only the prefix changes:

| | **venv** (your Mac) | **Docker** (the droplet, §4) |
|---|---|---|
| Prefix | `./.venv/bin/python -m …` | `sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m …` |
| Config | `--config argus.sandbox.toml` (or `argus.dev.toml`) — **required, no default** | **`--config /etc/argus/argus.$PROFILE.toml` required** |
| Audit DB | whatever `--config` selects, in the repo | `/data/argus-<...>.db` on the `argus-state` volume — **the profile decides which** |
| Best for | development, ad-hoc runs, analysis on your Mac | the always-on session that survives reboots |

> **The volume holds TWO databases, and the profile picks one.** Since the
> 2026-09-15 cutover `argus.live.toml` writes `/data/argus-live.db` (real orders,
> real money) while `/data/argus.db` sits beside it holding the **finished cert
> chain**. A command that names the wrong one does not error — it reads a real
> database and reports a quiet, empty day. Derive the path from `$PROFILE` the same
> way you derive `--config`, and see
> [§4](#running-the-tools-in-the-container) for the trap in full.

The **venv path is what §3 shows** and what you'll use day to day on your Mac.
The **Docker path is §4** — same commands, different prefix, a *separate*
database. Pick one per machine and know which DB you're pointed at; they don't
share data.

> `$PROFILE` in the Docker column is not a placeholder to fill in by hand — it is a
> shell variable you set once per session by reading `/etc/argus/host.env`, the file
> that decides which config this host runs. See
> [the preamble in §4](#the-profile-preamble); every droplet command below assumes it.

> Whichever you use, the file the tools open is set by `--config`, which is
> **required** — the tools read no environment variable and have no default.
> `argus.live.toml` = the LIVE audit log (real money, since 2026-09-15);
> `argus.sandbox.toml` = the cert audit log; `argus.dev.toml` = a throwaway dev DB.
> On the droplet, prefer `--config /etc/argus/argus.$PROFILE.toml` over naming a
> profile by hand — see [§1](#the-three-config-files--one-per-profile).

> **On your Mac, use `argus.dev.toml`.** Its `[server] bind_host` is `127.0.0.1`,
> so the kill endpoint binds fine locally. `argus.sandbox.toml`'s `bind_host` is
> the *droplet's* Tailscale IP — it won't bind on your Mac, and the run will fail
> loud with `ControlServerBindError` (§10). If you must run the sandbox config
> locally, point `[server] bind_host` at `127.0.0.1` or your Mac's own
> `tailscale ip -4` first.

### Start a shadow session

```bash
./.venv/bin/python -m argus.tools.shadow --config argus.dev.toml --minutes 390   # full day (~6.5h)
./.venv/bin/python -m argus.tools.shadow --config argus.dev.toml --minutes 30    # short test run
```

> **This is the dev path, not how the droplet collects data.** `tools.shadow` is
> a time-boxed run you start by hand and watch. On the droplet the always-on
> `argus.service` runs a session per trading day by itself (below) — the old
> `argus-shadow.timer` model is **retired**. Use `tools.shadow` for a bounded
> local run; don't reach for it to "start today's session."

(Docker equivalent — needs the service stopped, since it holds the writer lock, and
`$PROFILE` set as in [the preamble](#the-profile-preamble):
`sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.shadow --config /etc/argus/argus.$PROFILE.toml --minutes 30`.)

Startup sequence (each step prints; a failure stops the run):
1. Log in (two sessions: trading = cert, market data = per config)
2. Fetch account → **reconcile broker vs. expected positions** (mismatch refuses to run the session; in sandbox/live it also halts the process — §9)
3. Subscribe SPX, wait for spot
4. Load today's 0DTE SPXW chain, subscribe quotes + greeks
5. Report chain coverage, then run the loop

### Stopping a shadow run (there are no positions to close)

**`Ctrl-C`.** It unwinds cleanly — stops the loop, closes the streams and
sessions, drains the audit log, records `process.stop`. Give it a second; don't
double-tap unless it hangs. (In Docker: `Ctrl-C` on a foreground `compose run`,
or `sudo systemctl stop argus.service` for the service.)

> **In SHADOW there are no positions to close.** Shadow submits *nothing* and holds
> *nothing*, so "closing out" is not position management — it is just stopping
> the program. There is no end-of-day flatten, no cleanup. Think of the run as a
> **recorder**: `Ctrl-C` turns it off. That's the whole shutdown.
>
> ⚠️ **This does not describe the deployed live profile.** Stopping a live process
> that holds a position takes its watchdog, its virtual stop and its 15:30 flatten
> with it — the spread is then unmanaged until something brings the process back.
> Mid-session the control is **`POST /kill`** ([§9](#the-kill-switch--halt-from-your-phone)),
> which stops entries and keeps the watchdog. Stop the service only with a flat
> book, or knowing exactly what is open.

Each invocation is self-contained; the audit log (`argus.db`) persists across
runs and accumulates.

**Stopping a run does not score it.** Scoring — "marking" — is a separate step
you run *after* the market closes ([§6](#scoring-a-session-against-settlement)):
the run records *what it would have traded*; marking stamps on *what that was
worth*. They're deliberately decoupled, so marking can happen minutes or days
later. The run is the recording; marking is the grade.

### The daily rhythm

On the droplet the supervisor is **already running** — you start and stop
nothing. The day looks like this:

| When (ET) | What happens | You |
|---|---|---|
| — | Process is up, idle, holding the writer lock | nothing |
| Before the open | — | **check the account is flat** (see below) |
| ~09:40 | Session logs in, reconciles, subscribes the chain | glance at `/status` ([§9](#read-only-status--glance-from-your-phone)) |
| 10:00–15:00 | The entry window (`[signal] entry_earliest`/`entry_latest`). On a live profile this is where orders are placed; the watchdog adopts each fill and evaluates its triggers every second | nothing |
| ~15:00 | Entry window closes; loop logs `signal.none`. Anything held stays watched | nothing |
| **15:30** | **`FLATTEN_BEFORE_CLOSE`** — `[watchdog] flatten_before_close_s` before *that day's* close (12:30 on a half-day). Every still-open spread is closed through the ordinary exit path — **unless the flatten deferral holds it** (`watchdog_flatten_deferred`, a far-OTM spread whose insurance is overpriced; its stops stay live to the close) | on a deferral page, check the spot and strike on it by eye. **Intraday rollback:** a restart after 15:00 (12:00 on a half-day) disables the deferral for the rest of the day — the watchdog must have watched spot since then; the permanent one is `flatten_deferral = "off"` |
| Close | Session ends by itself; process returns to idle | nothing |
| After the close | — | the read-only reads (below) — `desk`, `evaluability`, `regime`; safe anytime, service up |
| After **16:15** | — | **preview** the day's scoring, read-only (below) |
| **16:30** | `argus-snapshot.timer` writes the nightly verified off-volume snapshot | nothing |
| **16:45** | `argus-eod.timer` renders the day's report and posts the digest ([below](#the-nightly-eod-report-argus-eodtimer)) | read the digest |
| Weekend | — | **write** the week's scorings in one stop window ([§6](#scoring-a-session-against-settlement)) |

> ### ⚠️ The one precondition: the account must be flat before the open
>
> The per-session reconcile compares the broker's **entire** position map against
> the `positions` table — which Argus writes itself, so a spread Argus opened
> yesterday and closed is expected and reconciles clean. What does not is anything
> Argus did not place, or a position whose record was lost.
>
> **On the deployed `live` profile a mismatch is a process-wide hard halt** — kill
> switch engaged, `source=reconciliation_mismatch`, and it will not trade again
> without a human. That is the correct behaviour and not something to route around:
> flatten the account or find the real discrepancy. **Only `shadow` is lenient**
> (that day aborts, an alert fires, the process survives for tomorrow).
>
> How WIDE the halt reaches is a second question: an order-level discrepancy
> confined to Argus's own working *opening* orders halts entries only and **keeps
> the watchdog**, so held positions keep their stops and their 15:30 flatten.
> Anything else is `ALL`.
> → [halt scope split](decisions/2026-08-29-halt-scope-split.md)

> **Two standing rules, because the traded account is dedicated to Argus**
> (`argus.live.toml` pins it; every account-derived rail reads a whole-account
> number and treats it as Argus-only):
> 1. **Nothing else trades this account — including by hand.** One foreign
>    position hard-halts the next restart, loudly and correctly.
> 2. **No deposits, withdrawals or transfers on a trading day.** `DailyPnl`
>    cannot tell a transfer from P&L, and a same-day restart re-uses the day's
>    original anchor — the contamination is not restart-clearable. Move capital
>    on a non-trading day.
> → [the account is dedicated](decisions/2026-08-31-live-account-is-not-dedicated.md)

#### After the close — the read-only reads

All four open the database `mode=ro`, take no lock and write nothing, so they are
safe with the service up and even mid-session. Run the preamble once per shell
first — `$PROFILE` comes from `/etc/argus/host.env`, and `ARGUS_ENV` is required by
**compose itself**, which interpolates it into the config mount and refuses when it
is unset ([the profile preamble](#the-profile-preamble)):

```bash
cd /opt/argus
PROFILE="$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)"
test -f "argus.$PROFILE.toml" && echo "profile: $PROFILE" || echo "STOP — no argus.$PROFILE.toml here"
```

```bash
# what traded, what it made, what it cost — from actual fills
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.desk \
     --config /etc/argus/argus.$PROFILE.toml

# how often the watchdog was blind, and the audit write rate by tier
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.evaluability \
     --config /etc/argus/argus.$PROFILE.toml

# the regime telemetry, one reading per session at a fixed ET clock
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.regime \
     --config /etc/argus/argus.$PROFILE.toml

# preview the day's scoring — reads the index close off the live feed
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.mark \
     --config /etc/argus/argus.$PROFILE.toml --dry-run --from-feed
```

Two properties of that last line are easy to trip over. `--from-feed` **refuses
before 16:15 ET** (a quote before then is not a settlement price; `--force`
overrides), and the feed read happens *before* the dry-run branch — so it pays the
quote wait either way. Earlier in the afternoon, use `--settlement` instead.

The `--config` path must be **absolute**: the container's working directory is
`/data`, so a relative `argus.$PROFILE.toml` resolves onto the state volume, finds
nothing, and stops. And never type the profile in by hand in place of `$PROFILE` —
[§4](#running-the-tools-in-the-container) has both traps in full, including why
`sandbox` hardcoded on a live droplet reads as a quiet, empty day rather than an
error.

**Should you mark at the end of every day?** No — and the distinction is
*preview* vs. *write*.

A **dry run is free** — it is the last line of the block above, and it confirms the
day produced something worth keeping. Use `--settlement` instead of `--from-feed`
when you already have the close, or when it is before 16:15:

```bash
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.mark \
     --config /etc/argus/argus.$PROFILE.toml --dry-run --settlement 6xxx.xx
```

Either way, write the SPX close down.

The **real write is not urgent, so batch it.** Marking is fully backfillable —
the proposal records are durable in the append-only chain, and marking appends
*separate* `shadow.outcome` records, so `--date` + `--settlement` works months
later. But the write takes the single-writer lock, and the supervisor holds
that lock for its whole life (not just during market hours), so every write-mode
mark costs a service stop/start. Doing that nightly buys a nightly bounce for no
benefit.

**Mark weekly, over the weekend**, when the scheduler is idle on a non-trading day
and stopping the service costs nothing — the procedure is in
[§6](#scoring-a-session-against-settlement). The only thing that expires is
`--from-feed` (same-day, after 16:15, and the weaker settlement source anyway), so
record the close each day and you lose nothing by batching.

### The always-on supervisor — `python -m argus`

```bash
ARGUS_ENV=dev ./.venv/bin/python -m argus    # always-on; Ctrl-C / SIGTERM to stop
                                             # (loads argus.dev.toml; there is no default)
```

**This is the main always-on path, and on the droplet it is the trading process.**
It runs one session per trading day by itself — on the market calendar in the
**loaded profile's** `[market]`
(weekends, NYSE holidays, and half-day early closes all suppress or shorten the
day) — from the open (`session_open`) to the close, and idles in between. **What
that session DOES is decided by the profile's `environment` key, not by this
command**: `shadow` records and submits nothing, `sandbox`/`live` trade. Outside
market hours it boots **without broker credentials** and just sleeps until the
next open; broker login only happens inside a session. (The one secret it needs at
boot is `ARGUS_KILL_TOKEN`, for the always-on kill endpoint — §9.) It logs in with a bounded, ban-aware retry
at the open (`[market] login_attempts` / `login_retry_interval_s`), so a cert that
is slow to wake no longer needs a systemd restart to ride out — and never crashes
the process. `Ctrl-C`/SIGTERM unwinds cleanly at any point (idle, mid-session, or
mid-login-retry).

`argus.tools.shadow` is a **thin, time-boxed wrapper** over the same session
pipeline — use it for a quick bounded run you watch and discard (§3 above); use
`python -m argus` for the real, continuous session.

> Add `ARGUS_CONFIG=argus.dev.toml` unless this is a real data-collection run.
> The dev tool bypasses the calendar (it runs a single session on demand); the
> calendar only gates the always-on `python -m argus` path.

**Day rollover.** Because the process now spans many trading days, per-day risk
state is rebuilt each session: on a NEW trading day the daily-loss breaker starts
un-tripped and the day-P&L anchor resets, so yesterday's loss never carries into
today. Within a day it is the opposite — a restart at 11:40 rehydrates the 11:20
trip and the day's ORIGINAL anchor from the audit log, so a crash or a deploy
cannot hand back a spent loss budget. The latch is keyed to the **trading date**,
not the process. The kill switch is the deliberate exception in the other
direction — if it is engaged (e.g. a failed audit write), it **stays engaged
across the rollover** until manually cleared. A rollover must never un-halt the
system.

---

## 4. Running in Docker (the droplet)

Local runs use the venv (§3). The **droplet** runs Argus in Docker under systemd,
so it survives reboots and the runtime is pinned — one image runs every profile,
with no dependency drift between them. **Since 2026-09-15 that droplet is trading
the real account**; the profile in `/etc/argus/host.env` is what decides.

> ### ⚠️ The container has its OWN databases — plural
>
> Container state lives on the Docker named volume `argus-state`, mounted at
> `/data`. Those are **different files** from the `argus.db` in your repo. Data
> from a containerized run will not appear in your local audit log and vice
> versa. Know which one you are looking at before you conclude a session
> recorded nothing.
>
> And the volume holds **two** chains: `/data/argus-live.db`, which the deployed
> `live` profile writes, and `/data/argus.db`, the finished cert chain plus the
> accumulating shadow/sandbox signal record. Reading the wrong one is silent — it
> is a real database that simply has nothing to say about today.

### What's where

| Thing | Location |
|---|---|
| Runtime config | `./argus.${ARGUS_ENV}.toml`, bind-mounted read-only under `/etc/argus` (profile from `/etc/argus/host.env`) |
| Credentials | `./.env` via `env_file:` — never in the image (incl. `ARGUS_KILL_TOKEN`) |
| Control endpoint | `http://<tailscale-ip>:<port>` — `POST /kill` (halt), `POST /resume?confirm=yes` (the only thing that clears one) + read-only `GET /status`, `GET /` (§9); `[server]` in the profile config, setup below |
| Audit log + WAL | Docker volume `argus-state` → `/data/argus-<profile-db>.db` (`argus-live.db` under `live`, `argus.db` under `sandbox` — `[audit] db_path` in the loaded profile) |
| Live logs | `journalctl -u argus.service -f` |
| Retained logs | `sudo ARGUS_ENV="$PROFILE" docker compose logs` (json-file, 10 MB × 5) |
| Off-volume backups | host path `/path/to/backups/argus-*.db` (see below) |

### The deployment model (read this first)

**`argus.service` is the unit to enable, and on this droplet it is the trading
process.** It runs the compose default, `python -m argus` — the always-on
supervisor, which owns the signal loop and runs one session per trading day itself
(on the `[market]` calendar, from the open to the close, idling in between). It
holds the single-writer lock on the loaded profile's database the whole time it is
up (`/data/argus-live.db` under `live`).

> **Migrating from the old timer model?** `argus.service` and the retired
> `argus-shadow.timer` **both take the writer lock**, so running both crash-loops
> the loser. **Disable the timer BEFORE enabling the service** (order matters):
> ```bash
> sudo systemctl disable --now argus-shadow.timer   # if it was ever enabled
> sudo systemctl enable --now argus.service
> ```

- **`argus-shadow.timer` is RETIRED.** The one-shot-per-day model it drove is
  superseded now that the loop is always-on. Don't enable it.
- **`argus-shadow.service`** stays as a **manual** convenience only — a bounded,
  out-of-band `tools.shadow` run. Stop `argus.service` first (writer lock), or
  just use `docker compose run --rm` (which doesn't collide with the service) —
  by hand that is `sudo ARGUS_ENV="$PROFILE" docker compose run --rm …`, since the
  unit gets the profile from its `EnvironmentFile` and your shell does not.
- **`argus-snapshot.timer`** — a nightly (16:30 ET) consistent, verified snapshot of
  the audit DB to a host path off the volume, so your NAS has something safe to pull
  (the interim durability story until Litestream; see "Backups" below).
- **`argus-eod.timer`** — the nightly 16:45 ET end-of-day report: the read-only reads
  run for you, a digest to Slack, the transcript archived beside the snapshots.
  Read-only throughout and independent of `argus.service`
  ([below](#the-nightly-eod-report-argus-eodtimer)).
- **`argus-pull-check.timer`** — a daily 09:00 ET check that the **NAS is still
  pulling** the snapshots, alerting on the symptom (nothing pulled) rather than on
  any particular cause. It exists because on 2026-08-09 a power cut wiped the NAS's
  crontab, the pull stopped, and **nobody noticed for seventeen days** — by which
  point the droplet's retention had rotated two days off the back that the NAS had
  never seen. A watcher has to live somewhere that survives independently of the
  thing it watches. → runbook §13 (private record, not published)

> **The deploy does NOT install any of these ancillary units** — a change to them
> reaches the checkout and nowhere else, and `argus-deploy.sh` says so in its plan.
> Install and enable them by hand (`argus-snapshot` below, `argus-eod`
> [here](#the-nightly-eod-report-argus-eodtimer), `argus-pull-check` in the runbook).

Install:

```bash
# Set the droplet clock to Eastern so log stamps and the snapshot timer read in
# exchange time (the app computes ET internally regardless of host TZ).
sudo timedatectl set-timezone America/New_York

sudo systemctl disable --now argus-shadow.timer 2>/dev/null || true   # retire the old model
sudo cp /opt/argus/argus.service \
        /opt/argus/argus-snapshot.service /opt/argus/argus-snapshot.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argus.service           # the always-on supervisor
sudo systemctl enable --now argus-snapshot.timer    # nightly 16:30 ET

systemctl list-timers 'argus-*'                     # confirm the snapshot next-fire
```

> **Keep the `[market]` holiday calendar current.** The market calendar isn't
> derivable from code, so the supervisor reads `holidays`/`half_days` from
> the profile config's `[market]`. Update those lists each year against the official NYSE
> calendar (config is bind-mounted, so `git pull` on the droplet is enough — no
> rebuild). A stale list would run a session on a market holiday.

Watch it:

```bash
journalctl -u argus.service -f              # the supervisor (sessions + idle)
journalctl -u argus-snapshot.service -f     # the nightly backup
```

Trigger the snapshot by hand with `sudo systemctl start argus-snapshot.service`.

### The kill-switch endpoint (Tailscale)

The always-on `POST /kill` halt control (§9) must be reachable from your phone and
**nowhere else**, so it binds the droplet's Tailscale interface only. Two settings
plus one networking fact make it live on the droplet:

1. **The loaded profile's `[server] bind_host`** — set to this box's Tailscale IP
   (`tailscale ip -4`), with a fixed `bind_port` (e.g. `8443`). All three shipped
   profiles carry the key; `dev` binds `127.0.0.1`. A wildcard
   (`0.0.0.0`) is **refused** by config — that would answer on the public
   interface. A `bind_host` that isn't actually up on the host refuses to bind and
   the process won't start (fail loud, never a wider fallback).
2. **`.env` `ARGUS_KILL_TOKEN`** — a long random bearer token (§1). Missing token →
   the process refuses to start.
3. **Host networking** — the container runs `network_mode: host` (already set in
   `docker-compose.yml`) because the Tailscale interface lives in the host network
   namespace; a bridged container can't bind it. Argus opens no other listening
   socket, so sharing the host netns exposes nothing else.
4. **A `tailscale0` firewall allow** — ufw default-denies inbound on *every*
   interface, so the port is blocked even over Tailscale until you allow it on
   that interface only (keeping it open to the tailnet, still denied on the public
   interface):
   ```bash
   sudo ufw allow in on tailscale0 to any port 8443 proto tcp   # match [server] bind_port
   ```

The port never listens on the public interface regardless (the bind is to the
*specific* Tailscale IP, not a wildcard — Argus enforces that), so the specific-IP
bind is the primary control and the ufw / DO Cloud Firewall default-deny is the
backstop.

That same server also hosts the **read-only monitoring routes** — `GET /status`
(JSON) and `GET /` (a phone-legible HTML view) — behind the *same* bearer auth on
the *same* `bind_port`, so the ufw allow above already covers them (no extra
firewall step). Verify reachability after deploy the **non-destructive** way — it
confirms the bind, the token, and that the process is alive, and changes nothing:

```bash
curl -fsS -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
     http://<tailscale-ip>:<port>/status    # read-only — does NOT trip anything
```

A `200` with a JSON body confirms end-to-end reachability. `401` = wrong/missing
token; a refused connection = Tailscale down, wrong `bind_host`, or bridged
networking. The `POST .../kill` equivalent (§9) proves the same reachability but
genuinely **trips the switch — and a trip is DURABLE**: it survives the day
rollover, a deploy, an OOM and a reboot, and is cleared by exactly one thing,
`POST /resume?confirm=yes`. On the live profile it also stops entries for real.
**Reach for `/status` unless you actually mean to halt.**

#### The one-tap phone trigger

The endpoint is only half of "reachable in one action, from a phone" — the other
half is the client. From a phone already on the tailnet (Tailscale app installed
and connected), the request is:

```bash
curl -fsS -XPOST -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
     http://<tailscale-ip>:<port>/kill
```

`<tailscale-ip>:<port>` are the exact `[server]` values from the profile config (e.g.
`http://192.0.2.10:8443/kill`); `tailscale ip -4` on the droplet prints the IP.

**The actual one-action mechanism is an iOS Shortcut** (or Android **Tasker**
task) wrapping that exact request — a "Get Contents of URL" action, POST method,
one header `Authorization: Bearer <token>`, that URL — pinned to the home screen
(and optionally the Lock Screen or an Apple Watch complication). Tap the icon, the
phone is already on the tailnet, the request fires. Store the token in the
Shortcut itself (the phone is the trusted device); **never** put it in a URL query
string. Keep the Shortcut's request identical to the curl above.

Pin a second "Argus status" icon next to it for the read-only `/status` route
(same token, same tailnet, GET instead of POST) — safe to hit anytime.

What the responses mean:

| Response | Meaning |
|---|---|
| `200 {"status":"tripped"}` | Halt engaged **and** the Tier 1 trip record is durable. New entries are refused at the gate and at the submit; an entry order already working is cancelled within one walk cycle. Exits, the watchdog and the process are untouched. |
| `200 {"status":"already_tripped"}` | The safe idempotent re-hit. |
| `401` | Bad or missing token. |
| `500 {"audit":"write_failed"}` | The switch engaged but the record didn't persist. **The halt still stands** — investigate the DB. |

Scope detail — what a trip does and does not stop — is in
[`docs/decisions/2026-08-03-kill-switch-scope.md`](decisions/2026-08-03-kill-switch-scope.md).

**How long "within one walk cycle" is, measured on live.** `_stop_reason()` is
checked at the top of every cycle, after the cadence hold, and every
`status_poll_interval_s` while an order is on its way to the market — but not
*during* the hold's sleep. At live's `entry_cadence_s = 4.0` and
`status_poll_interval_s = 1.0` that is **about 4–5 seconds** from the press to the
cancel going out. **A fill that lands inside that window is not a failure**: the
position is adopted by the watchdog, keeps its virtual stop, and is flattened at
15:30 like any other. `/kill` promises no NEW entries and that a working order is
chased — it does not promise flat.

#### SOP: you pressed `/kill` and an entry order may still be working

The machine handles this by itself, and since v0.4.5 that includes the live
re-cancel — P5 re-asks the broker every 15s, backing off to 900s, for as long as
the broker says the order is WORKING. This is for the cases it cannot reach.

1. **Read `/status`'s `unmanaged` section.** A residual row names the order, its
   identifier, how long it has been working, and the mode on the summary line
   (`auto-cancel ON for opening orders`). **An absent row is not an all-clear** —
   see step 3.
2. **Wait out two attempts before touching anything.** A cancel leaves an order at
   `Cancel Requested`, not gone, and the benign case goes terminal in about 1.5s.
   Act by hand when `residual_order_working` keeps paging past its second attempt,
   or in the cases below.
3. **Go to the broker directly, not to `/status`, in three cases.** The registry is
   in-process and **never rehydrated**, so any restart after a kill (deploy, OOM,
   reboot) leaves an order nobody in-process knows about — reconciliation halts
   entries at the next boot and pages, but nothing cancels it. Same if `/kill`
   itself was unreachable (tailnet down), in which case the broker's own app is the
   only kill switch there is. And same if the submit's ack was lost *and* the
   broker could not be read afterwards, so Argus never learned the order's id.
4. **Cancel it in the tastytrade app — OPENING orders only.** The identifier tells
   you which: `argus-open-<date>-<side>-<strikes>` may be cancelled;
   **`argus-close-…` may NEVER be**, because cancelling a close strands a position.
   The machine refuses that under any halt or grade, and the human inherits the
   same rule.
5. **Expect reconciliation to notice, and let it.** An order Argus placed that is
   now gone is the expected resolution, not an alarm. If it FILLED before the
   cancel landed, you have a position: the watchdog adopts it, stops it and
   flattens it — do not close it by hand as well, which would be the double-close
   the one-attempt rule exists to prevent.

The exit-side mirror of this — a position that cannot be closed *by* the machine —
is C5's SOP in
[`docs/go-live-findings.md`](go-live-findings.md), and its trigger is a
different page (`exit_spent_still_held`).

### Build

```bash
# EVERY compose command needs ARGUS_ENV: docker-compose.yml interpolates it into
# the config mount with `${ARGUS_ENV:?}` and REFUSES when it is unset. sudo scrubs
# the environment, so on the droplet it goes on sudo's own command line.
export ARGUS_ENV=sandbox                        # LOCAL only — an `export` does not
                                                # survive `sudo`; on the droplet use
                                                # the preamble under "Operate on the
                                                # droplet" instead

# The build arg stamps the image so /status can say what is running. $( ) runs in
# YOUR shell, not in the build — the image carries no .git and no git binary.
docker compose build --build-arg ARGUS_VERSION=$(git describe --tags --always --dirty)
docker tag argus:local argus:v<X.Y.Z>           # keep the release addressable
```

Python and every dependency are pinned by digest/hash, so an image tag is a
complete rollback unit — no dependency archaeology under time pressure.

**Omitting `--build-arg` is legal and reports `version: "unknown"`.** That is
deliberate — an ad hoc local build must not fail — and it is still the wrong answer
on a release: `/status`'s `version` field and the startup log's first line are how
"what is actually deployed?" gets answered without git archaeology. A `-dirty` suffix
means the build tree had modified tracked files; on the droplet that is usually the
in-place `argus.sandbox.toml` edit, so the page flags it amber rather than red.

**The `docker tag` line is not optional and is not a re-build.** Every
`docker compose build` overwrites `argus:local` in place, so an untagged release
is gone the next time you deploy — and `ARGUS_IMAGE_TAG=<previous>`, the image half
of the documented rollback, has nothing to point at. Checked on the droplet
2026-08-19: one row, `argus:local`. Recoverable (rebuild from the old git tag —
the build is deterministic), but not at incident speed.

### Run locally (to test the image)

```bash
cp .env.example .env && chmod 600 .env          # if you haven't already
export ARGUS_ENV=sandbox                        # which config file to mount. LOCAL
                                                # only: no sudo here, so an export
                                                # is enough
docker compose up                               # foreground; Ctrl-C to stop
docker compose up -d && docker compose logs -f  # background
docker compose down                             # stops; state survives
```

> `ARGUS_ENV` unset is a compose error, by design:
> `required variable ARGUS_ENV is missing a value`. There is no default anywhere
> in this chain — not in compose, not in the loader, not in the tools.

### Operate on the droplet

Repo at `/opt/argus`, `.env` root-owned mode 600. The deploy user is **not** in
the `docker` group, so everything goes through `sudo`.

<a name="the-profile-preamble"></a>
> ### Run this once per shell, before any `docker compose` command
>
> ```bash
> cd /opt/argus
> PROFILE="$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)"
> test -f "argus.$PROFILE.toml" \
>   && echo "profile: $PROFILE" \
>   || echo "STOP — '$PROFILE' has no argus.$PROFILE.toml here"
> ```
>
> The `test -f` is the whole check, and it is worth the line: it catches a typo, a
> profile whose config file is missing, and a `host.env` written with **quotes**
> (`ARGUS_ENV="live"` — systemd accepts it, this `sed` hands back `"live"` with the
> quotes still on, and compose would then look for `argus."live".toml`). Confirming
> the file exists answers all three at once, before a compose command does it for
> you in a less obvious way.
>
> **Every** compose command on the droplet needs `ARGUS_ENV` — `docker-compose.yml`
> interpolates it into the container env *and* into the config mount with
> `${ARGUS_ENV:?}`, and refuses when it is unset. That is on `build` and `run`, not
> only `up`: interpolation happens when the file is parsed, before any subcommand.
> And `sudo` scrubs the environment, so it goes on **sudo's own command line** —
> `export ARGUS_ENV=…` does not survive the `sudo`.
>
> Every droplet command in this README is written as
> `sudo ARGUS_ENV="$PROFILE" docker compose …`, reading `$PROFILE` from this block.
>
> **Read, never assume.** `/etc/argus/host.env` is per host and not in git — it is
> the one file the live cutover changes — so a hardcoded `sandbox` is a command that
> works today and silently targets the wrong config, or fails outright, the morning
> after the flip. For the same reason `--config` is written
> `/etc/argus/argus.$PROFILE.toml`: only *this host's* profile is mounted, so a
> literal `argus.sandbox.toml` is simply not there on a live box.
>
> Setting `ARGUS_ENV` in `./.env` would make all of this go away and **must not be
> done** — `.env` is hand-edited and untouched by the deploy, which is already the
> documented trap for `ARGUS_IMAGE_TAG`. The profile comes from `host.env`, which
> `argus.service` reads through `EnvironmentFile=`.

```bash
sudo systemctl start argus.service      # bring the stack up
sudo systemctl stop argus.service       # bring it down
sudo systemctl restart argus.service    # after a config or image change
sudo systemctl status argus.service     # is it healthy?
sudo journalctl -u argus.service -f     # live logs
```

> **`active (running)` is not the same as "trading."** Since v0.3.3 a process
> whose safety rail refused — a reconciliation mismatch, an unwritable audit log —
> **parks** rather than exiting: halted, unable to trade, and deliberately still
> serving `/status` and `/kill` so you can diagnose it from a phone. systemd shows
> it as active either way. What tells you it is halted:
>
> - a single `process_halted` Slack alert (once, not once per restart)
> - a **HALTED** row at the top of `/status`, with the reason and source
> - `still HALTED since …` repeated to the journal every 15 minutes
>
> **A parked process never resumes on its own.** Resolve the cause, then
> `sudo systemctl restart argus.service`. Before v0.3.3 the same condition exited
> into a restart loop with the control surface unreachable
> ([why](decisions/2026-08-17-halted-but-alive.md)).

First-time install — **the profile file first, or the unit will not start**
(`EnvironmentFile` carries no `-` prefix, deliberately):

```bash
sudo install -d -m 700 /etc/argus
printf 'ARGUS_ENV=sandbox\n' | sudo tee /etc/argus/host.env >/dev/null
sudo chmod 600 /etc/argus/host.env

sudo cp /opt/argus/argus.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argus.service       # starts on boot from now on
```

`/etc/argus/host.env` is one line naming this host's config profile. It is
per-host and **not in git** on purpose: **the live cutover is that file changing,
not a commit and not a rename.**

### Deploy a change — by git tag, and `git pull` is the wrong verb

**`scripts/argus-deploy.sh v<X.Y.Z>` runs the sequence below** (and, with an older
tag, IS the rollback — deterministic rebuild from source, no dependency on an image
tag still existing). `--dry-run` shows the plan without touching anything. It stamps
the build with the tag it verified and **reads the version back from the journal
afterwards**, failing the deploy if what came up is not what it built — the only
check for a restart that quietly reattached to the previous image. It also
resolves this host's profile from `/etc/argus/host.env`, refuses if the tag does
not carry that profile's config file, and **reads the config fingerprint back on
every release class** — which is the only post-restart assertion a config-only
release has. `--dry-run` prints the config diff, which on such a release *is* the
release. **It restarts only when the running process can actually see the change**
— a release touching only `scripts/`, `docs/`, `tests/` or another profile's config
is deployed by the checkout alone (`--restart` forces one), because a restart tears
down the session, the watchdog, the virtual stops and the marks. See
the runbook §17 (private record, not published)
for what it automates and why each guard exists. **Since v0.6.0 it does this for each
strategy's unit and restarts only a unit that is
RUNNING (`systemctl try-restart`): it never starts a stopped one** —
runbook §18 (private record, not published).

> **`git` NEVER takes sudo; `systemctl`, `docker` and `journalctl -u` always do.**
> The SSH deploy key belongs to `deploy-user`, so `sudo git fetch` dies with
> `Permission denied (publickey)`. `sudo git checkout` is worse — it *succeeds*
> (no network needed) and leaves root-owned objects in `.git`.

The droplet checks out a **release tag**, so it sits in detached HEAD permanently
and by design. `git pull` there fetches and then refuses to merge
(`You are not currently on a branch`) — the working tree is unchanged while the
output looks like something happened, and a `docker compose build` plus
`systemctl restart` on top of that both succeed and **silently redeploy the old
code**. That is as quiet as a deploy failure gets; it cost a confused hour on
2026-08-05.

```bash
cd /opt/argus
git fetch --tags                                 # NO sudo
git checkout v<X.Y.Z>                            # NO sudo

# VERIFY BEFORE BUILDING — the step that catches a no-op checkout.
git describe --tags                              # must be the tag you asked for
git status --short                               # must be clean

# The profile — every compose command needs it, and sudo scrubs the environment.
PROFILE="$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)"
sha256sum "argus.${PROFILE}.toml" | cut -c1-12   # the config sha you expect below

sudo ARGUS_ENV="$PROFILE" docker compose build \
     --build-arg ARGUS_VERSION=$(git describe --tags --always --dirty)
sudo docker tag argus:local argus:v<X.Y.Z>       # or the rollback has no target
sudo systemctl restart argus.service             # sudo; safe running or stopped

# CONFIRM BOTH HALVES. One line carries the image AND the config:
#   argus build v<X.Y.Z> config <profile>/argus.<profile>.toml @<sha>
sudo journalctl -u argus.service -n 50 | grep 'argus build'
```

> **Build before restart.** `argus.service` runs `docker compose up`/`down` with no
> `build` step, so a restart on its own quietly reattaches to the last image built —
> including its version label. Nothing errors; you just get the previous build,
> confidently labelled. The unit itself needs no change for any of this.

Before checking out, if the droplet has been touched by hand,
`git log --oneline origin/main..HEAD` must print **nothing**. If it lists
commits, someone committed on the droplet and a checkout would orphan them.

A **config-only release** (`argus.sandbox.toml`, `blackout_dates.txt`,
`docker-compose.yml` are all bind-mounted or read from disk) needs no
`docker compose build` — checkout plus `systemctl restart` is the whole deploy.
**The version on `/status` will legitimately still name the previous tag there**;
what verifies that release is the config fingerprint (`config` on `/status`, and
`@<sha>` on the startup line), which the deploy script now asserts against the
file in the tag. It is still a **restart** — config is read once at startup, so a
config release tears down the session, the watchdog and the marks exactly like a
code one. There is no hot-reload and none is planned.

> **New required config keys break a mismatched deploy, deliberately.** The
> config loader is fail-closed, so new code against an old config refuses
> to start with a `ConfigError` rather than defaulting. Chunks 3–5 each added
> required keys (`[execution]` walk params incl. `entry_drift_abort_points`, the
> whole `[watchdog]` section, `[risk] max_loss_per_position` and
> `max_contracts_per_position`), and `blackout_dates.txt` must move with the
> config too. Check out the tag before building, always.

### Roll back — both halves, or it isn't a rollback

Two tags, two jobs, not interchangeable:

- **The git tag** moves source *and* the bind-mounted `argus.<profile>.toml` /
  `blackout_dates.txt` together. Config is versioned by git on the droplet, **not**
  by the image.
- **`ARGUS_IMAGE_TAG`** rolls the image back without a rebuild.

```bash
cd /opt/argus && git checkout v<previous>        # code + config (NO sudo)
# set ARGUS_IMAGE_TAG=<previous-tag> in /opt/argus/.env, then:
sudo systemctl restart argus.service             # image
```

Rolling back only the image leaves new config against old code; rolling back only
git leaves old config against a new image. Neither half is a rollback on its own.

If a past deploy did use `sudo git`, clean up the root-owned objects it left —
they will eventually fail a plain-`git` write:

```bash
find /opt/argus/.git ! -user deploy-user | head
sudo chown -R deploy-user:deploy-user /opt/argus/.git   # if anything was listed
```

### Running the tools in the container

`--config` is **required** — the tools have no default at all — and `ARGUS_ENV`
is required by **compose itself** (it interpolates the config mount and refuses
when unset). Both come from `$PROFILE`; set it once per shell as in
[the preamble above](#the-profile-preamble).

Earlier revisions of this block dropped the prefix "for width" and told you to add
it back. That is the wrong trade for a block whose whole purpose is copy-paste: the
line you copy is the line that has to run. Every one below is complete.

```bash
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.quotes \
     --config /etc/argus/argus.$PROFILE.toml SPX
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.account \
     --config /etc/argus/argus.$PROFILE.toml
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.chain \
     --config /etc/argus/argus.$PROFILE.toml
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.mark \
     --config /etc/argus/argus.$PROFILE.toml --from-feed
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.desk \
     --config /etc/argus/argus.$PROFILE.toml
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.evaluability \
     --config /etc/argus/argus.$PROFILE.toml
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.regime \
     --config /etc/argus/argus.$PROFILE.toml
```

These share the volume, so `tools.mark` scores the same audit log the service
wrote. **Note:** the service holds the writer lock (§9), so a `compose run` that
needs to *write* while the service is up will be refused. Read-only tools are
fine; `tools.mark` needs the service stopped.

Two things about the `--config` argument specifically, both of which fail in ways
worth recognising:

**Pass it as an ABSOLUTE path.** The container's working directory is `/data` (the
state volume), not `/opt/argus`, so `--config argus.$PROFILE.toml` resolves to
`/data/argus.$PROFILE.toml`, which does not exist. Nothing is baked into the image
either — the config arrives only through the bind mount — so this is a clean
`ConfigError: config file not found`, not a silent fallback. The absolute path is
also what keeps the profile in the filename, which is what arms the
filename↔`environment` assertion: `/etc/argus/argus.live.toml` declaring
`environment = "sandbox"` is refused, and an ad-hoc `--config /tmp/x.toml` asserts
nothing at all.

**Never hardcode the profile.** `sudo ARGUS_ENV=sandbox … --config
/etc/argus/argus.sandbox.toml` is a working command on *any* host, including the
live one: `argus.sandbox.toml` is tracked in git, so it is sitting at `/opt/argus`
after the cutover too. Compose mounts it, the loader accepts it, and `db_path`
resolves to `argus.db` while the live process is writing `argus-live.db` on the same
volume — so `tools.desk` prints the last **cert** trading day on a live droplet with
nothing to say it is doing so. Deriving `$PROFILE` from `host.env` is the whole
defence; it costs one line in the preamble.

`tools.evaluability` is read-only (`mode=ro`, no lock) and safe to run **while the
service is up**. It answers two questions off one session: how often the watchdog
was fully unevaluable (`blind`) and for how long each episode lasted — the input to
the settle-allowance decision in
[the live-fire pass, Finding 5](decisions/2026-08-13-live-fire-sandbox-pass.md)
— and what the audit log actually writes per second, split by tier, which is the
number `[audit] synchronous`'s comment cites.

`tools.loop_lag` opens **no database at all** and is likewise safe while the
service is up. It measures event-loop lag under the account liveness refresh in
both shapes — the pre-2026-08-26 on-loop one and the threaded one that shipped —
against real TLS, defaulting to a local self-signed stub with a deliberately COLD
connection pool (`--live-cert` points it at the cert API instead, read-only GETs,
no orders):

```bash
./.venv/bin/python -m argus.tools.loop_lag --config argus.dev.toml --attempts 500
```

**Its output is a sanity check, not the acceptance evidence.** A loopback
handshake against a one-cert chain is the cheapest there is, and it separates the
two shapes by well under an order of magnitude on fast hardware. The real question
is answered from the droplet over one full session: *did `LoopHeartbeat.breach_count`
fall while `account.refresh_failed` volume stayed comparable?* — never "did the
pool stay warm", which the fix deliberately does not change.
→ [off the event loop](decisions/2026-08-26-account-refresh-off-the-event-loop.md)

### Running a bounded shadow session by hand

`argus.service` runs the daily session automatically. To run a **bounded** one
out-of-band — a partial day, a test, a re-run — invoke the tool directly. The
service holds the writer lock, so **stop it first** (or the lock refuses the run):

```bash
sudo systemctl stop argus.service                  # release the writer lock
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.shadow \
     --config /etc/argus/argus.$PROFILE.toml --minutes 120
sudo systemctl start argus.service                 # resume the always-on session

# The manual unit does the same bounded run (also needs the service stopped):
#   sudo systemctl start argus-shadow.service
```

### Host monitoring (DigitalOcean)

The droplet runs the **DigitalOcean Monitoring Agent** with **Resource Alerts**
sent to **email + Slack**. This watches host health — is the box starving the
always-on process? — and is separate from Argus's own audit-log alerts. Each
policy fires at **> 70% for 5 minutes**:

| Metric | Threshold |
| --- | --- |
| 5-minute load average | > 70% for 5 min |
| CPU | > 70% for 5 min |
| Memory | > 70% for 5 min |
| Disk | > 70% for 5 min |

Disk is the one that bites hardest: a full disk takes SQLite — and the
irreplaceable audit log — down with it. Configure these in the DO control panel
(Monitoring → Alert Policies); they are not part of this repo.

### Backups (off-volume snapshots)

`argus-snapshot.timer` writes a nightly **consistent, verified** snapshot to
`/path/to/backups/argus-<timestamp>.db` — a single self-contained file, taken
via SQLite's online-backup API (never a `cp` of the live WAL DB), `verify_chain`'d
before it counts, and kept off the volume so `down -v` can't take it too. Run one
now: `sudo systemctl start argus-snapshot.service`.

Pull them to your NAS with **the NAS initiating** (so the droplet holds no NAS
credentials). Three scripts are copied onto the NAS and scheduled there —
`scripts/argus-pull.sh` (private record, not published) (the pull itself, snapshots plus
the EOD transcripts), `scripts/argus-pull-daemon.sh` (private record, not published)
and `scripts/argus-pull-watchdog.sh` (private record, not published) (which restart
it, because that NAS rebuilds `/` from firmware on every boot and loses its crontab).
Full procedure: runbook §13 (private record, not published).
`scripts/pull-snapshots.example.sh` (private record, not published) is the
generic, un-deployed illustration of the same idea — read it for the shape, not as
the thing that runs.

Watching the watcher is `argus-pull-check.timer`, on the **droplet** (above): the
NAS-side mechanisms all share the fate of the device they run on, which is how
seventeen days of silence happened once already.

This is the interim durability story, and it is the copy that exists. **Litestream →
R2 was scoped as the before-live upgrade and did NOT ship before the flip** — it is
designed (`docs/designs/2026-09-03-litestream-r2-replication.md`) and not built, so
live is running on the volume plus nightly snapshots. Accepted, not forgotten.

> `docker compose down -v` (with the profile, like every compose command) still
> destroys the **volume** and the live chain. With
> nightly snapshots you'd lose at most a day — but use plain `down`, and keep the
> snapshots flowing to the NAS.

### The nightly EOD report (`argus-eod.timer`)

The four read-only commands in [§3](#after-the-close--the-read-only-reads), run for
you at **16:45 ET on trading days** — after `--from-feed`'s 16:15 floor and after
the 16:30 snapshot, so tonight's backup is a *section* of the report rather than
something to check tomorrow. A digest goes to Slack; the full transcript lands in
`/path/to/backups/reports/` beside the snapshots.

It is read-only throughout (`mode=ro`, no writer lock) and takes no dependency on
`argus.service`, so it is safe with the trading process up — and it must stay that
way: the **write**-mode `tools.mark` needs the service stopped, which an unattended
job must never do. Weekly marking is still a human act on a weekend.

**Install (the deploy does NOT do this** — like every ancillary unit, a change
reaches the checkout and nowhere else, and `argus-deploy.sh` shouts about it**):**

```bash
cd /opt/argus
sudo cp argus-eod.service argus-eod.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argus-eod.timer
```

Add `ARGUS_REPORT_SLACK_WEBHOOK_URL` to `/opt/argus/.env` first — its own channel,
never the alert one, so a nightly digest does not train you to skim where the Tier 1
pages land. Unset, it falls back to the alert webhook; with no webhook at all the
report is still rendered and archived, it just does not come to you.

**Run one now, or re-render a past day:**

```bash
PROFILE="$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)"
sudo ARGUS_ENV="$PROFILE" /opt/argus/scripts/argus-eod.sh
```

```bash
sudo ARGUS_ENV="$PROFILE" ARGUS_EOD_DATE=2026-09-16 /opt/argus/scripts/argus-eod.sh
```

**Which day an undated run reports:** the latest SETTLED session, not "today" — so a
catch-up after a reboot the next morning reports the day it missed, and a Saturday
run reports Friday. It renders each day ONCE: if that day's transcript already
exists it writes and posts nothing. `ARGUS_EOD_DATE` always renders and re-posts
(and skips the scoring preview for any day but today — `--from-feed` has no memory).

Two things to know about what it says. **A failed section still ships the report** —
the error is pasted in place and named in the digest, because a broken `desk` must
not cost you the flat check. And **the G1 numbers come from `gate.decision` rows, not
from `/status`**: the session clears the risk source on its way out, so at 16:45 the
endpoint carries no `nlv` and no `daily_pnl`. A day with no candidates therefore has
no series, and the report says *"no series"* rather than `0.00` — a structural zero
rendered as a number is the exact G1 failure.

**The NAS collects the transcripts** through a second arm in `scripts/argus-pull.sh`
(since v0.4.4), into `/path/to/nas/argus-pull/reports/`. It uses the same key
and the same rrsync root, so no `authorized_keys` change is needed. Copy the updated
script onto the NAS (runbook §13); until you do, the transcripts live on the droplet
only. `argus-pull-check` catches the NAS going dark, but **not** the reports arm
failing on its own; that shows only as `reports pull FAILED` in the NAS's `pull.log`.

#### The trading ledger and the Google Sheet

The **last** step of the same job, after the digest has been delivered: it rebuilds
the whole live ledger (every fill, trade and day since the cutover), reconciles it
against the broker's own transaction history per order id, writes five CSVs to
`/path/to/backups/reports/ledger/`, and replaces the data tabs of the Google Sheet.
A third arm in `scripts/argus-pull.sh` mirrors the CSVs to the NAS.
→ [design](decisions/2026-09-22-ledger-and-sheets.md)

**It can never fail the unit.** A non-zero exit from `argus-eod` means one thing —
the digest did not arrive — so a Google outage or a broker 502 posts a `:warning:` to
the **report** channel and leaves the exit code alone. A missing key or spreadsheet id
is a SKIP, with the CSVs still written: no channel is not a broken rail.

**Two files on the droplet, neither in git, both per host:**

```bash
sudo install -m 400 -o 10001 -g 10001 ~/argus-sheets-key.json /etc/argus/argus-sheets-key.json
printf 'ARGUS_LEDGER_SHEET=<spreadsheet id>\n' | sudo tee /etc/argus/ledger.env >/dev/null
```

The key is the Google **service account's** JSON (owned by uid 10001 because the
container runs as that uid, not root; `/etc/argus`, never `.env`, which is the
trading container's `env_file`). The spreadsheet id is the part of the sheet's URL
between `/d/` and `/edit`; the sheet must be shared as **Editor** with the key's
`client_email`. Prove both before the timer ever runs it:

```bash
python -m argus.tools.sheets check --key dev/argus-sheets-key.json --spreadsheet-id <id>
```

**The report tabs are built once, by hand**, and the nightly push never touches them
(it sends no formatting request at all, which is what lets them survive):

```bash
python -m argus.tools.sheets report --key dev/argus-sheets-key.json --spreadsheet-id <id>
```

That owns four tabs — `Dashboard`, `Weekly`, `Monthly` and a hidden `calc` — and a
rebuild REPLACES them. Put your own work on your own tab. The Dashboard carries a
period dropdown (`All` / `YTD` / `This month` / `This week` / `Last 30 days` /
`Custom`) that every number and chart follows.

**A spread held to expiry is valued at SETTLEMENT** — the day's last recorded spot,
via the same `shadow.outcome` math `tools.mark` uses — and the trade row names its
`gross_basis` (`settlement_spot`, or a refusal such as `spot_too_early` with a blank
gross). It counts in the Dashboard's Net P&L, beside an "Of which valued at
settlement" KPI; the broker reconciliation still compares fills only.
→ [decision](decisions/2026-09-22-valuing-a-held-to-expiry-trade.md)
To override the recorded spot with an official print, add one line to an optional,
persistent file (it must persist: the ledger rebuilds every night):

```bash
printf '2026-09-22,7753.21\n' | sudo tee -a /etc/argus/settlements.csv >/dev/null
sudo chmod 644 /etc/argus/settlements.csv   # the container reads it as uid 10001
```

Absent is the normal state. A malformed line costs its own date (`settlement_file_error`,
blank gross); a file that exists but is unreadable refuses EVERY settlement valuation
rather than guessing which dates it would have overridden — and `argus-eod.sh` warns
on the report channel when the file is not readable by the container's uid 10001. The
CSVs never go wrong silently either way — the ledger's journal line names each held
trade's basis and, for a recorded spot, when it was taken.

**A `DIFF` naming a trade under "P&L not from fills"** (`recon.pnl_mismatch`, detail
in `trades.pnl_check`) is Argus disagreeing with ITSELF, not with the broker: that
trade's gross is not the cash its own fills moved less what it owed at settlement. It
needs no broker read, and the broker's numbers can agree to the cent beside it.

**By hand, or to backfill after an outage** (the ledger is rebuilt in full every run,
so a re-run is idempotent and a missed night needs no catch-up logic):

```bash
sudo ARGUS_ENV="$PROFILE" docker compose run --rm -v /path/to/backups/reports/ledger:/ledger argus python -m argus.tools.ledger --config /etc/argus/argus.$PROFILE.toml --out-dir /ledger --broker
```

(With a settlements file, add `-v /etc/argus/settlements.csv:/settlements.csv:ro`
before `argus` and `--settlements /settlements.csv` at the end, as `argus-eod.sh` does.)

### Container-specific gotchas worth knowing

These are host assumptions the container surfaces, not bugs — flagged rather than
worked around.

- **`audit.db_path` is CWD-relative.** Each profile names a bare filename
  (`argus-live.db`, `argus.db`), so the image sets `WORKDIR /data` to land it on the
  volume without forking the config. Consequence: anything run inside the container
  from a *different* working directory silently opens a second, empty database
  instead of erroring.
- **Only `python -m argus` reads `ARGUS_ENV` / `ARGUS_CONFIG`.** Every
  `argus.tools.*` entry point **requires** `--config` — there is no default, so
  they fail with argparse's own "the following arguments are required" rather than
  quietly opening whatever config happens to be in the working directory.
  Pass `--config /etc/argus/argus.$PROFILE.toml` inside the container — **derived,
  never typed** (above).
- **The container runs `network_mode: host`, for the kill switch.** `POST /kill`
  binds the Tailscale IP, which lives in the host network namespace; a bridged
  container can't bind it, and Argus fails loud rather than falling back to a wider
  bind. Argus opens no other listening socket, so sharing the host netns exposes
  nothing else.
- **Credentials are visible in `sudo docker inspect argus`** and
  `/proc/<pid>/environ` to anyone with root or docker-group access on the host.
  That is inherent to `env_file:` — the guarantee this packaging makes is only that
  they are absent from the **image**. Keep droplet root access tight.
- **`TZ=America/New_York` in compose is cosmetic.** Argus computes the trading
  date/time explicitly in ET (`clock.py`) and never reads host-local time. TZ only
  makes log timestamps read in exchange time.
- **Litestream is not in the container.** The spec's SQLite replication story (§5)
  is still unbuilt — **including now that live capital is at stake** — so the volume
  plus the nightly snapshots are the only copies of the audit chain.
- **Keep the volume on local disk.** SQLite WAL is not safe over NFS or a network
  block device mounted with weak locking.

### Two things not to do

- **Don't set `ARGUS_LOG_LEVEL=DEBUG` on the droplet** — leave it unset, which
  is INFO (defaulted in both `__main__.py` and `docker-compose.yml`). It no
  longer leaks a bearer token: `__main__.py` pins the tastytrade/httpx/websockets
  loggers to WARNING regardless of this level, so the AlertStreamer frame's
  session token stays out of the on-disk log and journald. But DEBUG is very
  noisy, and that pin only covers the loggers we knew to name. For protocol
  debugging use `tools.shadow --verbose` on an interactive session you can watch
  and discard. Full detail: `docs/argus-deployment-runbook.md` §17.
- **Don't bind-mount the repo over `/data`.** The named volume is what keeps the
  container's database separate from your local one.

---

## 5. Reading the console output

The noisy tastytrade SDK logging is silenced by default (add `--verbose` to
restore the full websocket firehose for debugging). What you'll see:

### Chain coverage (right after startup)

```
waiting for chain data ...
   quotes 118/120   greeks 118/120
```

**Quotes and greeks are counted separately on purpose** — the strategy picks
strikes by *delta*, so missing greeks blocks every signal while quotes look fine.

| You see | Meaning |
|---|---|
| `quotes 120/120  greeks 118/120` | ✅ Both feeds live — signals can fire. |
| `quotes 120/120  greeks 0/120` | Quotes entitled, **greeks not** — options (OPRA/greeks) entitlement gap. No signal can fire. |
| `quotes 0/120  greeks 0/120` | Nothing subscribed — feed or symbol problem. |
| Counts plateau below total | Normal — far-wing strikes may not quote. |

### Status line (every ~15s)

```
  spot=7453.2 chain q=120/120 g=118/120 usable=118 md_age=0.2s acct_age=6.1s nlv=100000 | {...counts...}
```

| Field | Healthy | Wrong |
|---|---|---|
| `spot` | Moving, matches a real SPX quote | Frozen, or ~15 min stale (delayed feed) |
| `q=` / `g=` | Both near `total` | `g=0` → no greeks (see above) |
| `usable` | > 0 | `0` → no contract has both quote + greeks |
| `md_age` | < 2.0s (the staleness threshold) | Climbing → feed stalled |
| `acct_age` | Cycles 0–10s (REST liveness refresh) | Climbing unbounded → account link dead |
| `nlv` | Your account value | `0` → account state missing |

The trailing `{...}` is a running count of audit categories (see below).

### Alerts

Lines like `ALERT loop_lag_breach {...}` are the event-loop-lag early warning. A
one-off breach at startup (chain parse) is normal. Sustained breaches mean the
process is stalling — worth investigating.

> **No console in front of you?** The same vitals — phase, staleness ages,
> heartbeat lag, kill/breaker state, today's raw signal count — are served as JSON
> (`GET /status`) or a phone-legible HTML page (`GET /`) from the always-on control
> endpoint, reachable anywhere on the tailnet (§9). It's the remote view of this
> status line.

---

## 6. Querying the audit log (SQL)

Everything is recorded in the audit log (SQLite, WAL mode — **safe to read while a
run is in progress**, from another terminal).

> **Substitute the right database.** The queries below are written against
> `argus.db` because that is the local/cert filename. **On the droplet since
> 2026-09-15 the traded chain is `argus-live.db`**, and `argus.db` beside it is the
> *finished* cert chain — so a query naming `argus.db` there succeeds and reports a
> quiet, empty day rather than erroring. Read `[audit] db_path` out of the profile
> you mean, or take the path from `$PROFILE`.

> **Open it read-only.** Every query below uses `?mode=ro`. A read-write handle on
> the live log is how accidents start, and read-only costs you nothing — WAL readers
> never block the writer.

### Audit categories

**Tier 1** (crash-critical, written before the next action):

| Category | When |
|---|---|
| `session.configured` | Startup — records which environments (cert/live) were used |
| `session.login_gave_up` | Every OAuth login retry attempt exhausted — no session runs today |
| `session.aborted` | Session ended cleanly via `SessionAborted` (a clean "no session today") — payload carries why |
| `session.error` | An unexpected exception broke a trading day's session body outside the paths already handled gracefully — the process idles to the next day rather than crashing |
| `session.event_blackout` | Today is a macro-event blackout day (FOMC, or a discretionary date; CPI/NFP/PCE are omitted by policy and TRADED since v0.5.4). Written at session start, **before** anything reads the list, so a hand edit mid-session cannot make the record disagree with what the day did. Entries stand down; the watchdog, the exits and the flatten all run |
| `session.entries_ended` | Today's entries stood down while the session kept running — the `EntriesEnded` latch (2026-09-01). **Not a halt**: no kill switch, no `HaltRecord`, and it clears at the rollover with no human step |
| `session.monitor_only` | The session came up able to watch but not to enter — a `HaltGrade.ENTRIES` halt in force. Held positions keep their virtual stops, their marks and the 15:30 flatten |
| `process.start` / `process.stop` | Process boundaries |
| `process.halted` | A rail refused and the process halted. Carries the `source` and the reason. Under `HaltGrade.ALL` the process then PARKS (halted, serving `/status` and `/kill`, unable to trade); under `ENTRIES` it keeps running and keeps watching |
| `reconcile.result` / `reconcile.skipped` | Broker reconciliation outcome |
| `reconcile.expired_positions` | One or more `missing_at_broker` rows resolved by their own past OCC expiry — written **before** the ledger delete, carries per-row verdicts. → [expired positions](decisions/2026-08-12-expired-position-resolution.md) |
| `reconcile.sandbox_reset` | The sandbox 24h wipe actually cleared the ledger (day rolled over, nothing written today) |
| `reconcile.unreachable` | **Gave up trying to REACH the broker** — never "reached it and disagreed", which is `reconcile.result`. Written ONCE per episode, after `[reconcile] retry_budget_s` is spent (or the operator stopped it): carries `leg`, `outer_attempts`, `elapsed_s` and `gave_up`. At process start the process then PARKS; per trading day the day aborts. → [broker unreachable](decisions/2026-08-26-reconcile-broker-unreachable.md) |
| `signal.candidate` | A signal fired (full spread detail) |
| `gate.decision` | Pre-trade gate allow/deny + reasons |
| `shadow.would_submit` | **Shadow only: the order that WOULD have been placed.** One per qualifying cadence tick |
| `entry.dry_run` | **Dry run only: the order that was validated and discarded.** One per candidate. Carries the same `signal`/`proposal` block as the row above, which is what makes a dry-run day scoreable — see [§6](#scoring-a-session-against-settlement) |
| `entry.halted` | Kill switch engaged between gate-pass and order construction — the candidate is refused, rare by construction |
| `entry.not_built` | A gate-approved candidate could not be turned into an order (price aged past tolerance, credit has no expressible limit) — session continues, may re-propose next tick |
| `entry.finished` | How a submitted entry's price walk ended (filled / cancelled / aborted / never-worked / etc.) — one per attempt, and for non-fill outcomes the only place the reason is written. **Also the live path's scoreable record**: carries the same `signal`/`proposal` block as the two rows above (2026-08-13) |
| `entry.dry_run_failed` | An unexpected error abandoned one dry-run candidate; the session continues |
| `entry.aborted_day` | An unexpected error ended today's entry session outright — nothing was submitted, no kill switch (there is no order state to protect) |
| `entry.guard_unreachable_abandoned` | The duplicate-order guard could not be reached across a whole bounded burst, so that trade idea was retired. The **day** ends only after `execution.guard_unreachable_max_streak` such bursts back to back, and the streak counts DISTINCT TRADE IDEAS, never ticks |
| `shadow.outcome` | **What the idea was WORTH** (written by `argus.tools.mark`, from any of the three proposal rows above). Settlement for contracts held to expiry; for a position the watchdog closed intraday, the **realized** result from its `order.fill` prices — see [§6](#scoring-a-session-against-settlement) |
| `order.submit_attempt` | An order is about to be sent to the broker (dry run or real) |
| `order.submitted` | The broker acknowledged a submitted order |
| `order.submit_failed` | A submit call raised — outcome genuinely unknown (a timeout can mean it routed anyway); reconciliation is the backstop |
| `order.cancel_attempt` | A cancel request is about to be sent |
| `order.cancel_requested` | The broker accepted a cancel request — the order is at CANCEL_REQUESTED, not yet confirmed gone |
| `order.cancel_failed` | A cancel call raised |
| `order.replace_attempt` | A reprice/replace is about to be sent |
| `order.replaced` | The broker acknowledged a replace, which mints a new broker order id |
| `order.replace_failed` | A replace call raised |
| `order.unmappable` | An account-stream order frame could not be parsed into an `Order` — may have been a fill that will not be recorded; reconciliation is the backstop |
| `order.unmappable_live` | The same, but on the **polled** `get_live_orders()` path, which reconciliation and the duplicate guard both read. These are carried explicitly rather than dropped, and are a required argument to `reconcile_orders` that halts — "did Argus place this?" and "can Argus read this?" are different questions |
| `order.foreign` | An order event arrived whose identifier Argus did not mint |
| `order.fill` | A leg movement was booked to the position store — one per leg, written after the store commit |
| `order.duplicate_blocked` | The duplicate-order guard refused a submission |
| `order.residual_unmanaged` | An order a walk abandoned is **still working at the broker** past `watchdog.unmanaged_order_max_s`. This is the kill switch's "already working at the exchange" promise failing to land, and it pages |
| `order.residual_filled` | An abandoned order was seen FILLED by the recheck rather than cancelled — the position is real and gets adopted, watched and flattened. **Never written for a resting market-crossing close** (`working_expected`, 2026-09-25): a cross filling is the cross doing its job and the position is CLOSED, so it resolves at Tier 2 as `order.residual_resolved` with `sighting: filled`, and does not page |
| `order.event` | **Shadow** (`session.py`): any order event at all is unexpected there, so every one is Tier 1. **Sandbox/live** (`fills.py`): Tier 1 the FIRST time an order is seen terminal-with-nothing-filled (`disposition: "dead"` — rejected, cancelled, expired), Tier 2 for everything else. Carries `reject_reason`, the broker's own words, since 2026-08-27 — see note below |
| `walk.started` | A price walk began for a freshly-sent order |
| `walk.finished` | How a walk ended (filled / cancelled / aborted / never-worked / etc.) |
| `walk.disconnected` | A broker call mid-walk raised — recorded before the recovery decision that follows it |
| `watchdog.exit_started` | A triggered exit's closing order is about to be sent |
| `watchdog.crossed` | The exit walk gave up holding a price and crossed at a market-crossing limit. Since 2026-09-25 it carries `walked_away_at` = the rung the walk STOPPED at, with `opening_limit`, `walk_steps` and `limit_basis` (`natural` / `past_ladder` / `natural_no_long_bid` / `width`) beside it. **A row without `opening_limit` is pre-fix and carries the OPENING limit under `walked_away_at`**, right only where the walk had 0 steps. The cross is then registered as a resting CLOSE residual: paged only past `unmanaged_order_max_s`, never cancelled, and named on the spent-exit page and `/status` as `working_close` |
| `watchdog.exit_finished` | An exit order that actually reached the broker concluded |
| `watchdog.book_agrees` | Broker/ledger open-leg counts reconciled after a prior disagreement (falling edge) |
| `watchdog.book_disagreement` | Broker and ledger open-leg counts disagree (rising edge). `booked_but_absent` fires only after it outlives `watchdog.book_settle_s`; `held_but_unbooked` fires on the first observation **unless** Argus's own recent closing fills account for the excess legs — evidence, never a timer (2026-08-20) |
| `watchdog.unwatchable` | An open position exists that the order ledger cannot turn into a watchable spread — an unwatched position, the gap this module exists to close. **It gets no trigger evaluated, no mark and no 15:30 flatten** while still consuming a concurrency slot, which is why it has its own `/status` row |
| `watchdog.watchable_again` | A previously unwatchable position became attributable and rejoined the watch list (falling edge) |
| `watchdog.leg_imbalance` | An open spread's legs don't net to a clean position |
| `watchdog.entry_credit_incoherent` | A position's REALIZED entry credit came back non-positive, so the credit stop has no sane basis. The position **stays watched** on the limit basis and pages — marking it `malformed` would strip a live vertical of its triggers, its flatten and its `/status` row over a price anomaly |
| `watchdog.book_cache_over_reporting` | The per-shape book-confirm cache answered for more than it should have — the bound on how many blind windows one cached verdict may cover |
| `watchdog.unevaluable` | A watched position has **no** trigger evaluable — nothing is watching it (rising edge). One unevaluable trigger while the other still runs is `watchdog.degraded`, Tier 2, and does not page |
| `watchdog.evaluable_again` | A previously-blind position has at least one trigger back (falling edge). Fires even if still degraded — it closes the episode the page opened; `unknown_reasons` says what is still missing |
| `watchdog.triggered` | A virtual-stop trigger condition sustained past its debounce threshold (rising edge) |
| `watchdog.exit_unpriceable` | A trigger fired but the legs could not be priced, so no exit order could be built (rising edge) |
| `watchdog.exit_unattempted` | An exit's closing order was never sent (nothing reached the broker), so the trigger stays latched (rising edge) |
| `watchdog.exit_still_unplaced` | **A triggered position has had no order in the market for `watchdog.exit_unattempted_max_s`.** The bound on SILENCE for both no-order paths, **repeating every `exit_unattempted_max_s` until the condition clears** (not once — a single page is one missed notification away from silence). `unresolved_for_s` grows across repeats, so the trail reads as a duration — `condition` says which (`unattempted` / `unpriceable`). The retry has not stopped and is not meant to; this says it is getting nowhere |
| `watchdog.exit_nothing_sent` | An exit attempt ended with the broker confirming **no order exists** — nothing reached it, so the attempt is retried through `DuplicateOrderGuard` under the 3-send `_EXIT_SEND_ATTEMPTS` budget. The guard is the licence, never the outcome's own answer |
| `watchdog.exit_cross_unpriceable` | The market-crossing escalation could not build a price (no offer on the short leg). Retryable on the same budget, because it is reachable only THROUGH `_await_settled`'s positive confirmation that the prior order is dead |
| `watchdog.exit_spent_still_held` | **A triggered position's one exit attempt is spent and it is still held.** Repeating. Nothing automated will try again — this is the page that means *go and close it by hand*. The position is still evaluated and still marked; only the SUBMISSION is suppressed. Since 2026-09-24 the page carries `stub_close`: `available` means the partial's stub still has its ONE further close coming (it waits for the partial's fill to BOOK, and a refused ask is retried one refresh interval later), `sent`/`exhausted` means the page and the 15:30 flatten are what is left. Since 2026-09-25 it also carries `working_close` (`broker_order_id`, `limit_price`, `working_for_s`) when a CLOSE — normally the market-crossing limit a `crossed` exit left resting — is still working at the broker: **check the broker before sending anything by hand**, or the manual close stacks on the working one (C5's double close) |
| `watchdog.exit_crashed` | The exit task itself raised. The attempt is spent IN MEMORY FIRST as `NOT_RESOLVED` with a `crash_error` sentence, so the next tick cannot send a second closing order for a position whose first may or may not have landed. The flatten's one-shot override is untouched — a crash must not GRANT one |
| `watchdog.flatten_override` | A position whose exit attempts are spent is being granted ONE further send past the mark, because the 15:30 flatten fired. Rising edge per position, never per tick; safe because `DuplicateOrderGuard` re-asks the BROKER and permits only DEAD |
| `watchdog.stub_close` | **A partial close's stub is getting its ONE further close** (2026-09-24). Fires only once the ledger holds FEWER contracts than the close was sized for — a close whose fills have not booked is not a stub. Tier 1, written ONCE, BEFORE the first handoff — the durable spend the boot read rehydrates, so a crash loop cannot stack a second allowance-backed send; a re-handoff after a refusal that sent nothing is Tier 2 `watchdog.stub_close_retry`. The continuation walks the ORIGIN's ladder (a profit-take stub re-takes, an emergency stub re-closes), switching to the exit ladder if a stop, the backstop or the flatten is firing, through the guard's quantity rule. Past 15:30 it yields to the flatten's own allowance |
| `watchdog.stub_close_exhausted` | The duplicate guard refused the stub's close three times (paced one refresh interval apart; `guard_reason` says why — the remainder still working, a fill not booked, an unreadable opener). Nothing was sent; the allowance stays spent and the spent page and the 15:30 flatten are what is left. One page |
| `watchdog.stub_close_unattempted` | The stub's close could not be BUILT (e.g. a profit take whose mark has left one tick, a mid that rounds to nothing, the guard unreachable). Nothing was sent; retried one refresh interval later. One page per episode |
| `watchdog.stub_close_unreadable` | The boot read of today's durable stub spends could not be read: no continuation may fire this session (the kill-state posture — refuse, never halt), positions stay paged with the flatten backstop. One page |
| `watchdog.flatten_deferred` | **The 15:30 flatten was DEFERRED for this position** (2026-09-25): its insurance was at least `k` times overpriced (`k·P_touch(z)·W ≤ one tick`). Written ONCE per position per day, paged in `on` (`acted: false` in `record`). Carries every input: `z`, `p_touch`, `lhs`, `sigma_implied`, `sigma_realized`, `distance_points`, `k`/`tail_z99`/`tail_alpha`. The credit stop and the underlying trigger stay live to the close; the deferral is re-checked every tick. Check the spot and strike on the page by eye |
| `watchdog.flatten_undeferred` | **A deferral was released and the flatten handed to the exit path on the same tick**; `reason` says why (`insurance_not_overpriced`, `another_trigger_fired:<reasons>`, `broker_poll_stale`, `underlying_trigger_blind`, …). Tier 1 and paged the FIRST time each distinct reason releases a position; a repeat of a reason already on record is Tier 2 `watchdog.flatten_undeferred_again` |
| `watchdog.flatten_deferral_outcome` | **The reliability dataset** — once per position deferred at any point that day: `touched` (spot reached the barrier, or the underlying trigger fired, after the first deferral), minimum z and distance, the PRICE triggers that fired after it, and `ended`. `ended: session_end` with `seconds_to_close` ≈ 0 is the NORMAL close (the scheduler stops the session AT the close); a larger `seconds_to_close` is a SIGTERM or an aborted session; `left_the_ledger` means it closed first. **A `touched: true` row is a recalibration trigger** — rerun `tools.flatten_calibration` |
| `order.duplicate_close_permitted` | **The guard permitted a close by QUANTITY** (2026-09-24): a FILLED order existed under the `argus-close-*` key and the broker's own per-leg fills proved `closed_filled + q <= opened_filled`. Tier 1 with the arithmetic — the inverse of `order.duplicate_blocked`, and the post-mortem's proof for every second close under a filled key |
| `watchdog.recovered` | The watchdog completed a clean pass after failing — it is watching again. Entries stay halted until `POST /resume` |
| `watchdog.degraded` | **(Tier 1 sense)** The watchdog itself is failing — an exception on the check path. Since 2026-09-10 this engages the kill switch at `HaltGrade.ENTRIES` and **keeps watching**: entries stop permanently, exits and the 15:30 flatten run on. See the split-personality note below; the Tier 2 row of the same name means something else entirely |
| `watchdog.fatal` | The watchdog hit an unrecoverable exception on a path that cannot continue |
| `kill_switch.trip` | Kill switch engaged — payload `source` says how: `http_endpoint` (POST /kill), `audit_log` (failed write), `heartbeat` (sustained stall), `reconciliation_mismatch` (sandbox/live broker-vs-expected mismatch; payload carries the discrepancy `detail`). **Also THE durable halt state**: a starting process is engaged iff the newest one of these has no `kill_switch.resume` at a higher `seq` |
| `kill_switch.resume` | A human cleared the halt via `POST /resume` — payload carries what it cleared (`cleared_source`/`cleared_reason`), the peer IP, whether a restart is still required, and any operator `reason`. **Never written speculatively**: a resume row's `seq` relative to the newest trip's IS the engaged/not-engaged answer |
| `kill_switch.restored` | This process came up on a halt it did not impose. Not a second trip (nothing was engaged — a halt was *found* engaged), so `/status`'s `since` still reports the original press |
| `kill_switch.restore_failed` | The durable state could not be READ, so the switch engaged **fail-closed** with a synthesized reason. "I cannot say whether a kill is in force" must never resolve to "it isn't" |
| `daily_loss.breach` | Daily loss breaker tripped |

> **Two categories have a split personality, and both are facts about the current source rather than documentation gaps.**
>
> `order.event`: Tier 1 when shadow's account-stream hook sees it (any event there is unexpected), and in sandbox/live **Tier 1 on the first terminal sighting, Tier 2 otherwise**. Same category name, two call sites, three tiering rules, by design.
>
> `watchdog.degraded`: **two unrelated meanings** sharing one name. At **Tier 2** it is one trigger unevaluable while the other still runs — routine, never paged. At **Tier 1** (added 2026-09-10) it is *the watchdog itself is failing*, which pages and halts entries. When reading the log, the tier disambiguates; when reading `/status`, so do the rows — `Exit triggers` renders the first, the `Watchdog` row the second.
>
> **The terminal promotion (2026-08-27).** A `dead` order is the END of a trade idea and in the common case the only record that names the broker's verdict — the walk logs its own outcome, but not why the broker refused. Tier 2 is queued and droppable, which is right for a status flap and wrong for that, so the whole point of capturing `reject_reason` would be lost if the record carrying it could be dropped under queue pressure. It fires ONCE per broker order id (the AlertStreamer re-delivers current state on reconnect, and 2026-08-13 put 1,517 Tier 1 writes in the log from one flapping condition); repeats still carry the reason, at Tier 2. Deduped on the broker order id and never on `external_identifier` — a walk reprices, so several orders share one identifier and the second rung's rejection would be swallowed. **`FILLED` is deliberately excluded**: terminal in the lifecycle sense, and the one status that must never be filed next to a cancellation. Its record is `order.fill`.
>
> Nothing DECIDES on `reject_reason` and nothing should — it is broker prose that changes without notice, and a rail built on matching it would fail open the day it does. `classify_order` reads `status` alone. The field's whole job is to be present when a human asks why.

**Tier 2** (high-volume telemetry):

| Category | When |
|---|---|
| `session.login_recovered` | Login succeeded after at least one prior failed attempt |
| `session.login_attempt_failed` | One OAuth login attempt failed and will retry |
| `reconcile.expiry_check` | Rows missing at the broker, but none excused by expiry — the halt stands; this records *why* each survivor wasn't excused |
| `reconcile.reset_check` | Sandbox 24h reset evaluated, no clear performed (the every-morning case) |
| `reconcile.unreachable_attempt` | One reconcile fetch attempt failed and will be retried — `leg`, `attempt`/`attempts`, `error_type`. The per-attempt half of the row above; a recovered blip leaves only these |
| `signal.none` | Evaluated, no signal — **carries the near-miss + rejection reason** |
| `signal.skip` | No chain available to evaluate |
| `entry.already_attempted` | A candidate re-proposed on a later cadence tick after already being attempted — the expected steady state for the rest of the day |
| `entry.entries_ended_today` | A candidate arrived after the `EntriesEnded` latch was set — refused, session continues |
| `entry.guard_unreachable_attempt` | One duplicate-guard round trip failed inside a bounded burst and will be retried. The per-attempt half of `entry.guard_unreachable_abandoned`; a recovered blip leaves only these |
| `walk.guard_unreachable_attempt` | The same, on the EXIT path. The bound there is on **RATE, not COUNT** — the opposite of the exit's send budget, because a guard check is a GET and costs no submission, and a count would leave a position unmanaged at 15:30 for a broker that recovered at 15:00 |
| `order.duplicate_check` | The duplicate-order guard checked and allowed a submission |
| `order.direction_disagreement` | The broker's reported leg action disagreed with the direction Argus minted. **Telemetry, never a rail** — tastytrade labels each leg relative to the ACCOUNT'S position, so on a chained book an opening BUY on a strike another live spread is short comes back as a CLOSE, and nothing is wrong |
| `order.residual_registered` | An order a walk abandoned was registered for recheck. Every `_request_cancel` is immediately followed by one, so a failed best-effort cancel is by construction a registered residual. **In-process and never rehydrated** — this prevents the state, it does not resolve a previous process's orphan |
| `order.residual_resolved` | The broker confirmed a registered residual is terminal — it is gone. Includes a resting market-crossing close that FILLED (`working_expected: true`, `sighting: filled`) — see `order.residual_filled` |
| `watchdog.flatten_deferred_again` | A position whose deferral had been released was deferred again — possible only when the released flatten sent nothing (a mid that rounds to zero, the modal far-OTM case) |
| `watchdog.flatten_undeferred_again` | A deferral was released for a reason already on record at Tier 1 for that position — the boundary flicker of a position hovering at `k·P·W ≈ one tick` on a book that cannot be built. Still handed to the exit path on the same tick |
| `watchdog.flatten_deferral_failed` | An exception while DECIDING a deferral. The flatten proceeds exactly as it would have — the one swallow in that path, because the fail-safe direction is the action |
| `order.residual_would_cancel` | The recheck decided to re-cancel but `watchdog.unmanaged_order_cancel` is `false`, so nothing was sent. **Live has been `true` since v0.4.5**; this is what cert records |
| `order.residual_cancel_failed` | A re-cancel was sent and raised. Bounded on RATE, not count — a budget of three would be spent inside an outage and never retried after recovery |
| `walk.filled_under_identifier` | A walk was about to conclude without a fill, asked the identifier before concluding, and found one. A fill on ANY rung means the spread is flat — three separate defects came from concluding without this question |
| `walk.holding` | A walk is between steps, waiting out its cycle |
| `walk.step` | A walk repriced and resubmitted |
| `watchdog.check` | Per-position, per-cadence trigger evaluation |
| `watchdog.check_spent` | The same evaluation, for a position whose exit attempt is **spent**. A separate category, and `watchdog.check` stops the instant it takes over — so **any reader that names one must name both**, which is exactly the closed-world assumption that made `tools.desk` show a quiet held row through a failed exit on 2026-09-14 |
| `watchdog.book_disagreement_still` | Broker/ledger disagreement persists past the first (edge) record — also every suppressed observation, carrying `settling_s`: inside the `booked_but_absent` settle allowance, or (`held_but_unbooked`) while `closing_fill_legs` names the legs Argus's own exit just closed |
| `watchdog.book_settled` | A disagreement cleared inside its allowance and never paged — `direction` says which, and `settled_after_s` is what `watchdog.book_settle_s` should be re-tuned from |
| `watchdog.unevaluable_still` | A position stays fully blind past the first (edge) record |
| `watchdog.degraded` | **(Tier 2 sense)** One trigger unevaluable, the other still live (rising edge) — routinely the credit trigger losing a stale wing quote while the underlying backstop runs on. Recorded, never paged; `watchdog.check` carries the per-tick detail. Not the Tier 1 category of the same name |
| `watchdog.degraded_still` | The watchdog is **still failing** past the first (edge) record — the Tier 2 continuation of the Tier 1 `watchdog.degraded` |
| `watchdog.triggered_still` | A trigger stays sustained past the first (edge) record |
| `watchdog.exit_unpriceable_still` | Legs stay unpriceable past the first (edge) record |
| `watchdog.exit_unattempted_still` | An exit stays unattempted past the first (edge) record |
| `watchdog.unwatchable_still` | A position stays unattributable past the first (edge) record |
| `watchdog.unwatchable_settled` | An unwatchable symbol resolved inside `watchdog.book_settle_s` and never paged |
| `watchdog.flatten_override_still` | The flatten's past-the-mark allowance is still outstanding past its rising edge. One override can span several handoffs and at most one becomes an order |
| `watchdog.working_orders_unread` | The `get_live_orders()` poll failed. **One failed poll blinds BOTH** the concurrency cap's working-orders term and the residual recheck, so the `/status` residual row goes quiet with it. Never halts the watchdog — the count only gates new entries |
| `watchdog.residual_recheck_failed` | The residual recheck itself raised |
| `watchdog.note_fills_disagreement` | A fill's broker-reported direction disagreed with the minted one while booking. Telemetry; the mint wins |
| `account.position_evicted` | A leg absent from REST for `staleness.account_position_evict_after_s` was dropped from the cache mirror. **Age-gated, never absence alone** — the broker's book trails a fill by ~4-5s and dropping a real leg would release a concurrency slot |
| `marketdata.disconnected` / `marketdata.reconnected` | DXLink link dropped / recovered (entries blocked in between) |
| `account.stream_disconnected` / `account.stream_reconnected` | AlertStreamer link dropped / recovered |
| `account.refresh_failed` / `account.refresh_recovered` | REST liveness re-seed failed / recovered |
| `account.reauth` | Account session re-authenticated after a token error |
| `account.reauth_failed` | An account session re-auth attempt itself failed |
| `control.auth_rejected` | A `POST /kill` arrived with a bad/missing bearer token — records the peer IP, **never** the token |

### Watch a run live (second terminal)

macOS doesn't ship `watch` by default. Either install it (`brew install watch`)
or use a plain shell loop, which works everywhere:

```bash
while true; do
  clear
  sqlite3 'file:argus.db?mode=ro' "SELECT category, COUNT(*) FROM audit_log GROUP BY category;"
  sleep 10
done
```

(`Ctrl-C` to stop the loop — it only stops the *watching*, not the shadow run.)

### What would/did fire

```bash
# The shadow "orders" — what the system would have submitted
sqlite3 'file:argus.db?mode=ro' "SELECT ts, payload FROM audit_log WHERE category='shadow.would_submit';"

# Every candidate that was generated
sqlite3 'file:argus.db?mode=ro' "SELECT ts, payload FROM audit_log WHERE category='signal.candidate';"

# Gate denials with reasons
sqlite3 'file:argus.db?mode=ro' "SELECT ts, payload FROM audit_log
  WHERE category='gate.decision' AND payload LIKE '%\"allowed\": false%';"
```

### Tuning `min_credit` from near-misses

Every `signal.none` records the short/long strikes, deltas, the credit it *would*
have paid, and which threshold rejected it. To see recent near-misses:

```bash
sqlite3 'file:argus.db?mode=ro' "SELECT ts, payload FROM audit_log
  WHERE category='signal.none' AND payload LIKE '%credit_below_min%'
  ORDER BY seq DESC LIMIT 10;"
```

Pretty-print and pull the credit distribution with Python:

```bash
./.venv/bin/python - <<'EOF'
import sqlite3, json
c = sqlite3.connect("file:argus.db?mode=ro", uri=True)
credits = []
for (p,) in c.execute("SELECT payload FROM audit_log WHERE category='signal.none'"):
    d = json.loads(p)
    for side in (d.get("per_side") or {}).values():
        if "credit" in side:
            credits.append(side["credit"])
if credits:
    credits.sort()
    for thr in (0.40, 0.50, 0.60, 0.75, 1.00):
        n = sum(1 for x in credits if x >= thr)
        print(f"  min_credit={thr:.2f} -> {n:5d} would-fire ({100*n/len(credits):.0f}%)")
else:
    print("no near-miss credits recorded yet")
EOF
```

That answers "if I set `min_credit` to X, how many signals fire?" directly from
the day's data.

### Scoring a session against settlement

Firing rate is only half the picture. `argus.tools.mark` supplies the other half:
what each candidate was actually **worth**. This is a separate step from running
or stopping a session — see the run-vs-mark distinction in [§3](#stopping-a-shadow-run-there-are-no-positions-to-close).

#### What marking reads — and why the command is the same in every environment

Each environment writes its own terminal record: **shadow** writes
`shadow.would_submit`, a **dry run** writes `entry.dry_run`, and **live fire**
writes `entry.finished`. Marking reads **all three**, as a union, and decides which
a date holds by looking at the records on that date — never at what the config
says today. So:

- **the command and its arguments are identical either way.** There is no flag,
  and nothing to remember when the environment moves;
- **flipping back to shadow needs nothing here.** Marking keeps working, in both
  directions, with no gap;
- **an old date is scored by what was recorded on it,** which is what makes
  backfilling a date months later correct rather than dependent on the config as
  it stands at the time you get round to it.

One thing looks different on a dry-run day, and it is not a fault: **the `reprop`
column reads `1`.** Shadow re-proposes a qualifying spread on every 5s tick and
records each one, so `reprop` counts a real entry window. The entry path records
once per candidate — it claims the spread on the first attempt and every later
tick is a no-op — so there is nothing to count. The credit, strikes, size and P&L
are unaffected: the credit recorded is the price the order was actually built at.
For the entry-window question on a dry-run day, count `signal.candidate` rows
instead. → [decision record](decisions/2026-08-12-markable-dry-run-records.md)

> **Dry-run days before 2026-08-12 cannot be marked.** `entry.dry_run` did not
> carry the strikes and credit until then, so 2026-08-11's 8 ideas — and any
> other pre-fix dry-run day — score as zero. Marking such a date is harmless (it
> logs an INFO line per skipped row and reports 0 ideas), it is just empty. This
> was a deliberate call not to backfill; the recovery path, if it is ever wanted,
> is written up in the decision record above.
>
> **Live-fire days before 2026-08-13 are different, and DO score.** `entry.finished`
> did not carry the block either, but a position that filled left `order.fill`
> records — and `candidate_id` is `(date, side, short, long)` by construction, so
> the idea is reconstructed from the fills and scored on the normal path. The table
> flags those rows with `reprop` = `0`. What is lost is only the counterfactual for
> ideas that never filled.

#### On a live day, settlement is not always the answer

A position the watchdog **closed intraday** has a realized result the 4:00 PM print
says nothing about. On 2026-08-13 a 7825/7830 call spread opened at 0.55 credit and
closed at 1.20 debit: **-$130 on 2 contracts, on a day the index settled where the
spread would have kept the full credit.** So marking scores each idea by what
actually happened to it:

| the day's record shows | scored as | column `how` |
|---|---|---|
| no fills (shadow, dry run, or a walk that walked away) | settlement, on the PROPOSED size | `unfilled` |
| opened and bought back | **realized**, from the fill prices | `closed_intraday` |
| opened and still held at 4:00 PM | settlement, on the FILL credit | `held_to_expiry` |
| part closed, part expired | both terms, summed | `partly_closed` |
| opened, but the broker reported no fill prices | **not scored** — prints `-`, excluded from the total, flagged loudly | `unpriceable` |

Fill prices, never the order's limit price: that exit filled at 1.20 against a
-1.30 limit, and the limit would have overstated the loss by $20. The footer also
reports what those positions **would** have been worth held to settlement — the
difference is what the virtual stop cost or saved, which is what
`watchdog.credit_multiple` gets tuned on.

`--settlement` / `--from-feed` is still required on a day where everything closed
intraday. The settlement term is multiplied by zero held contracts and the number
is simply unused; one command, no flag saying which kind of day it was.

#### What "settlement" is

For 0DTE SPXW, **settlement is where the SPX cash index closed at 4:00 PM ET**
that day. SPXW weeklies are *PM-settled* — they expire at the regular index
close — so the one number marking needs is the SPX closing value. From it the
outcome is deterministic (no fills, no path dependence):

- **put** spread wins full credit if SPX closed **above** the short strike;
- **call** spread wins full credit if SPX closed **below** the short strike;
- in between the strikes, partial; past the long strike, max loss.

#### How to get that number — two paths

**Supply it by hand (the normal path on the droplet).** Look up the SPX close and
pass `--settlement`. This works for any date, so it is what batched weekly marking
uses — see "When to mark" below.

**Same day, automatic.** After **16:15 ET** on the trading day, `--from-feed`
reads the current SPX index quote off the live feed and uses it as the close, so
you never look anything up:

```bash
./.venv/bin/python -m argus.tools.mark --config argus.sandbox.toml --from-feed
```

It waits until 16:15 so the closing print has settled, and it only works for
*today* — the feed has no memory of past closes. This is the **observed** index
close, not Cboe's official settlement print; fine for tuning, and what the
same-day path uses anyway. Note it is wrong on a **half-day** (which settles at
13:00), and on the droplet it is a *writing* run like any other, so it costs a
service stop during a weekday evening — which is why `--settlement` is the
default habit and `--from-feed` the convenience for a one-off you're watching.

**A past date, or you missed the window — supply it by hand.** The feed can't
reach back, so look up the SPX close yourself and pass `--settlement`. Get the
number from your tastytrade platform (pull up SPX / `$SPX` and read that date's
daily close) or any quote source — it's simply the S&P 500 index closing value:

```bash
# score a specific past date with a settlement you looked up
./.venv/bin/python -m argus.tools.mark --config argus.sandbox.toml --date 2026-07-22 --settlement 7508.15

# preview first — reads read-only, writes nothing, safe even mid-session
./.venv/bin/python -m argus.tools.mark --config argus.sandbox.toml --date 2026-07-22 --settlement 7508.15 --dry-run
```

> Substitute the real number for `7508.15`. Don't paste `<...>` placeholders —
> the shell reads `<` as redirection and errors. Always `--dry-run` first: it
> echoes the settlement and the resulting P&L so you can confirm the number
> landed where you meant before writing anything.

#### When to mark, and the Docker procedure

Marking is **not time-critical** — proposal records are durable and marking
appends *separate* `shadow.outcome` records, so any date can be scored later. But the write takes the single-writer lock, and the supervisor holds that
lock for its entire life, idle or not (§9). Every write-mode mark therefore costs
a service stop/start, which is why the recommended cadence is a **free dry-run
daily, and the real writes batched weekly over the weekend** — the scheduler is
idle on a non-trading day, so the stop costs nothing:

```bash
cd /opt/argus
sudo systemctl stop argus.service
```

Then once per trading date (`--date`/`--settlement` are single-valued, so a week
is five invocations):

```bash
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -m argus.tools.mark \
     --config /etc/argus/argus.$PROFILE.toml --date 2026-07-28 --settlement 6xxx.xx
```

```bash
sudo systemctl start argus.service
```

The restart is cheap in shadow: process-start reconcile records
`reconcile.skipped`, and the scheduler sees no trading day and sleeps to the next
open. Forget the stop and you get a clean `DatabaseLockedError` telling you to
stop the session or use `--dry-run` — nothing is damaged. The nightly
`argus-snapshot.timer` (16:30 ET) opens the DB read-only and never contends for
the lock, so it needs no coordination either way.

#### Reading the result

**Read the `idea` count, not the record count.** A 5s cadence re-proposes the
same spread all day, so one session's 456 `shadow.would_submit` records were
really 10 distinct spreads — and, under the realistic policy of taking the first
signal per side, **2 trades**:

```
rank  side strikes        credit   x    liab        P&L  result       window (ET)     reprop
   1  call 7505/7510       0.95   4    0.00     380.00  full_credit  10:00:04-10:19:35    29
   2  call 7510/7515       0.82   4    0.00     330.00  full_credit  10:01:04-10:43:30    61
   1  put  7440/7435       0.60   4    0.00     240.00  full_credit  10:01:14-10:23:11    51
   ...
10 idea(s), 10 win / 0 loss, total P&L 2,550.00
first-signal-per-side only (2 idea(s)): P&L 620.00
```

Scoring all 456 records would report 456 samples of a strategy that took two
positions — correlated noise dressed up as evidence, and the fastest way to tune
a threshold onto nothing. `rank` is the entry order within a side; `reprop` is
how many times the same spread came back, which measures how long the entry
window stayed open rather than how often you'd have traded — and which reads `1`
on a dry-run day, for the reason given above.

**Correcting a wrong settlement.** Marking refuses to score a date twice by
accident. If you marked with the wrong price (a placeholder, a bad lookup), re-run
with the real number and `--remark`:

```bash
./.venv/bin/python -m argus.tools.mark --date 2026-07-22 --settlement <real_close> --remark
```

`--remark` **appends** a fresh scoring — the log is append-only, so the original
rows are kept, not overwritten. A re-marked date then has *two* sets of
`shadow.outcome` rows (the wrong one tagged `remark: false`, the correction
`remark: true`), and a naive query would sum both.

Don't filter on the `remark` flag to disambiguate — the correct scoring might be
either value (a first mark is `false`; a correction is `true`). The robust rule
is **latest scoring per idea wins**: keep only the highest-`seq` outcome for each
(date, rank, side). The review queries below use that form, so they stay correct
whether or not a date was ever re-marked. Simplest of all, though: get the
settlement right the first time — always `--dry-run` to eyeball the P&L before
writing.

Once several days are marked, the tuning question becomes a single query. It
keeps only the **latest** outcome per idea (highest `seq`), so a re-marked date
contributes its correction once, not both scorings:

```bash
sqlite3 'file:argus.db?mode=ro' "
  SELECT json_extract(a.payload,'\$.side')                       AS side,
         COUNT(*)                                                AS trades,
         ROUND(AVG(json_extract(a.payload,'\$.credit')),2)       AS avg_credit,
         SUM(json_extract(a.payload,'\$.win'))                   AS wins,
         ROUND(SUM(json_extract(a.payload,'\$.pnl')),2)          AS pnl
  FROM audit_log a
  WHERE a.category='shadow.outcome'
    AND json_extract(a.payload,'\$.entry_rank')=1
    AND a.seq = (SELECT MAX(b.seq) FROM audit_log b
                 WHERE b.category='shadow.outcome'
                   AND json_extract(b.payload,'\$.trading_date')=json_extract(a.payload,'\$.trading_date')
                   AND json_extract(b.payload,'\$.entry_rank')  =json_extract(a.payload,'\$.entry_rank')
                   AND json_extract(b.payload,'\$.side')        =json_extract(a.payload,'\$.side'))
  GROUP BY side;"
```

Drop the `entry_rank=1` filter to score every distinct idea; add
`AND json_extract(a.payload,'\$.credit') >= 0.75` to ask what a higher
`min_credit` would have done to both the count and the P&L. If you never
re-mark, the inner `MAX(seq)` subquery is a no-op and you can omit it — but
leaving it in is always correct.

In Docker the DB is on the volume and the image has no `sqlite3` CLI, so query
it through Python instead (same latest-wins logic):

```bash
sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -c "
import sqlite3, tomllib, os
# Derive the DB from the profile — /data holds two chains and naming the wrong
# one reads clean and reports an empty day.
p = os.environ['ARGUS_ENV']
db = tomllib.load(open(f'/etc/argus/argus.{p}.toml','rb'))['audit']['db_path']
c = sqlite3.connect(f'file:/data/{db}?mode=ro', uri=True)
sql = '''
  SELECT json_extract(a.payload,\"\$.side\"), COUNT(*),
         SUM(json_extract(a.payload,\"\$.win\")),
         ROUND(SUM(json_extract(a.payload,\"\$.pnl\")),2)
  FROM audit_log a WHERE a.category=\"shadow.outcome\"
    AND json_extract(a.payload,\"\$.entry_rank\")=1
    AND a.seq=(SELECT MAX(b.seq) FROM audit_log b WHERE b.category=\"shadow.outcome\"
      AND json_extract(b.payload,\"\$.trading_date\")=json_extract(a.payload,\"\$.trading_date\")
      AND json_extract(b.payload,\"\$.entry_rank\")=json_extract(a.payload,\"\$.entry_rank\")
      AND json_extract(b.payload,\"\$.side\")=json_extract(a.payload,\"\$.side\"))
  GROUP BY 1'''
for row in c.execute(sql): print(row)"
```

### Integrity

The log is a hash chain — tampering is detectable. Verify:

```bash
./.venv/bin/python -c "
import sqlite3
from argus.audit.log import AuditLog
c = sqlite3.connect('file:argus.db?mode=ro', uri=True); c.row_factory = sqlite3.Row
AuditLog(c, tier2_queue_maxsize=1, tier2_queue_alert_depth=1).verify_chain()
print('audit chain intact')"
```

This is a full rehash of every row — seconds on a large log, and it is **not**
run automatically at startup. Run it after a crash, after restoring a backup, or
whenever you want the tamper check to actually mean something.

---

## 7. All commands

Every tool **requires** `--config` and takes `--verbose`, with one exception —
`tools.blackout_dates` (below) reads no Argus config at all.

> **One tool here can place an order**, and only when told to: `tools.order_smoke
> --live-submit` submits, replaces and cancels once, deliberately at an unfillable
> price. Everything else is read-only or writes only to the audit log. Point it at
> `argus.dev.toml`, run it outside the session window, and **verify the account is
> flat afterwards, not just before** — on the live profile it would route to the
> real account like anything else.

> **`--config` is the safety knob, and it no longer has a default to get wrong.**
> A default path is the mechanism of the 2026-07-21 incident, so v0.3.17 deleted
> it: an invocation that names no config now fails with argparse's own "the
> following arguments are required". Pass
> `--config argus.dev.toml` for anything that isn't a real data-collection run.
> Inside the container it is `--config /etc/argus/argus.$PROFILE.toml` — **absolute**
> (the working directory there is `/data`) and **derived, never typed** (`sandbox`
> hardcoded still resolves on a live droplet, and reads as an empty day). See
> [§4](#running-the-tools-in-the-container).

| Command | Purpose |
|---|---|
| `python -m argus.tools.shadow [--minutes N] [--account NUM]` | Time-boxed shadow run you start by hand — signal → gate → would-submit logging. **Dev path**; the droplet uses the supervisor below. |
| `python -m argus.tools.mark (--from-feed \| --settlement PRICE) [--date D] [--dry-run]` | **Score a session.** Marks the day's candidates to settlement and records `shadow.outcome`. `--dry-run` is read-only and safe anytime; the write needs the service stopped (§6). |
| `python -m argus.tools.desk [--date D]` | **The desk view.** What is held, what closed intraday and what it made (from actual fills), fees, repricing cost per entry, gate denials by reason. Read-only, no lock, safe mid-session. Does NOT do settlement — held positions show their last intraday mark and point at `tools.mark`. |
| `python -m argus.tools.regime [--from D] [--to D] [--at HH:MM]` | **The regime telemetry reader** — the instrument `[regime]` gets calibrated from. Estimator health (and which of the three NaN modes), one reading per session at a fixed ET clock, the distribution with blackout days excluded, and **realized vol beside it** — RV, IV/RV and the volatility signature plot, computed off the recorded index path. Read-only, no lock, safe mid-session. Prints the decision rule; never a pasteable `[regime]` block. |
| `python -m argus.tools.replay_selection (--config C \| --db FILE) --date D [--expect-entries d1,d2,…] [--extract-fixture PATH]` / `--fixture PATH` | **The mid-delta selector's pre-deploy gate** (2026-09-21). Re-runs `signal/implied.py` over a day's recorded `signal.candidate` + near-miss `signal.none` legs (bid/ask exist on rows from 2026-09-21 on) and checks the four criteria: solver success ≥99% on two-sided OTM legs, put fresh-greeks median \|mid − feed\| ≤ 0.005, the day's entries reproduce `--expect-entries` to ±0.001, every reconstructed payload encodes. Exits 1 on FAIL. `--db` reads a pulled snapshot; `--extract-fixture` writes the committed CI fixture (market quotes only). Read-only, no lock, safe mid-session. |
| `python -m argus.tools.evaluability [--date D]` | **The blind-episode + write-rate instrument.** How often the watchdog was fully unevaluable and for how long each episode lasted, plus what the audit log writes per second split by tier. Read-only, no lock, safe mid-session. |
| `python -m argus.tools.loop_lag [--attempts N] [--live-cert]` | Measures event-loop lag under the account liveness refresh, on-loop vs threaded, against real TLS. **Opens no database at all** — safe with the service up. A sanity check, not acceptance evidence (§4). |
| `python -m argus.tools.quotes SYMBOL... [--seconds N]` | Stream live quotes for symbols; prints price + measured staleness. Verifies the market-data feed. |
| `python -m argus.tools.chain [--underlying SPXW] [--limit N]` | Dump the option chain: expirations, roots, strike coverage, streamer symbols. Diagnoses "no 0DTE" problems. |
| `python -m argus.tools.account [--account NUM]` | Fetch positions + balances and run reconciliation. Verifies the account layer. |
| `python -m argus.tools.credcheck` | Check credential *shape* (never prints values). Diagnoses login failures. **Shape, never authentication** — only a real session proves a registration works. |
| `python -m argus.tools.order_smoke [--live-submit]` | Order-primitive smoke check against the configured broker. **The one tool here that can place an order**, and only with `--live-submit`. Run it on `argus.dev.toml`, outside the session window, and verify the account is flat *afterwards*. |
| `python -m argus.tools.eod [--date D]` | Render the end-of-day report — the read-only reads plus the snapshot and journal sections, as one transcript + Slack digest. Normally invoked by `scripts/argus-eod.sh` under `argus-eod.timer` (§4), not by hand. Read-only. |
| `python -m argus.tools.blackout_dates --out blackout_dates.txt --omit CPI --omit NFP --omit PCE [--months N] [--check] [--add-fomc D] [--add-discretionary D]` | **Regenerate the macro-event blackout file** — FRED (CPI/NFP/PCE) merged with hand-maintained `fomc_dates.txt` and optional `discretionary_blackout_dates.txt`. **Since v0.5.4 always pass `--omit CPI --omit NFP --omit PCE`** ([decision](decisions/2026-09-28-trading-the-8-30-release-days.md)): the 8:30 releases are traded, still fetched and checked, and written as commented records; the file's header names the flags and a test fails if they are dropped. FOMC can never be omitted. `--months` sets the forward horizon from today (default `12`) that bounds both the FRED query and the two hand-maintained lists; each source's own calendar publishes only so far ahead, so `--months` beyond that shows as `<-- runs out early` in the per-event summary — not a bug, the cue to extend `fomc_dates.txt` or just re-run closer to the default. `--check` reports whether the file would change and exits non-zero, writing nothing. Regenerate when `/status`'s "Event blackout" row goes amber (inside `BLACKOUT_WARN_DAYS`, 30 days of runway) or red (exhausted); commit and deploy the regenerated file **with** the config (§6, config-only release — `blackout_dates.txt` is bind-mounted like `argus.<profile>.toml`). **The one command on this page with no `--config`** — it never loads an Argus profile; needs `ARGUS_FRED_API_KEY` instead. |
| `python -m argus` | **The always-on supervisor, and on the droplet the trading process.** Runs one session per trading day on the `[market]` calendar; idles outside market hours (no broker login there — but needs `ARGUS_KILL_TOKEN` at boot for the kill endpoint, §9). What the session *does* is the profile's `environment` key, not this command. |
| `python -m pytest` | Run the test suite. |

**Recommended pre-flight before a long run** — all on the dev config, so a
pre-flight check can never disturb the log you're about to fill:

```bash
./.venv/bin/python -m argus.tools.quotes  --config argus.dev.toml SPX   # feed live & real-time?
./.venv/bin/python -m argus.tools.chain   --config argus.dev.toml       # 0DTE present today?
./.venv/bin/python -m argus.tools.account --config argus.dev.toml       # reconciles clean?
```

**And after the close** — an unmarked session is unscored data. Preview daily
(read-only, service stays up), write the batch weekly ([§6](#when-to-mark-and-the-docker-procedure)):

```bash
./.venv/bin/python -m argus.tools.mark --dry-run --settlement 6xxx.xx
```

---

## 8. Live vs. sandbox data

Set in the profile's `[marketdata] source`. **This is a separate axis from
`environment`** — the data feed and the order destination are chosen independently,
which is the whole reason this section exists:

| `source` | Data feed | Use |
|---|---|---|
| `"sandbox"` | Cert. **15-min delayed**, streams on weekdays, static on weekends. | Validating mechanics. Useless for judging signal quality. |
| `"live"` | Production, **real-time, READ-ONLY**. | Measuring actual signal quality. Needs the `TASTYTRADE_MARKETDATA_LIVE_*` credentials. **All three shipped profiles use this**, and have since 2026-07-20. |

**Reading live market data is not trading live** — a market-data session cannot
place an order, hold a position, or put money at risk. **Where orders go is decided
by `environment` alone**: `sandbox` routes to cert, `live` routes to the production
account, and since the 2026-09-15 cutover the droplet's profile says `live`. So the
live feed has been on all along and means nothing about the order destination —
read `environment`, never `[marketdata] source`, to answer "is this real?".

The split is enforced structurally, not by convention: `tt.trading_session` RAISES
on a market-data session, so nothing that can move money can hold the (possibly
live) read-only one, and the two draw from different credential slots
([§1](#1-one-time-setup)).

To confirm you're actually getting real-time (not delayed) live data, run
`argus.tools.quotes SPX` and compare against a known real-time SPX quote. The
websocket URL in `--verbose` output also tells you: `.../realtime` vs `.../delayed`.

---

## 9. Safety behaviour you will actually see

These are the rails that fire visibly and change what the system will do. **All of
them are working as designed** — they are not bugs to route around. Two stop it
outright (the writer lock, a broken audit log), one narrows it without stopping it
(a watchdog exception), and the last two are the controls you drive by hand (`/kill`
and `/resume`), alongside the read-only `/status` view.

### One writer per database, enforced

Argus takes an exclusive lock on the audit database at startup. A second process
targeting the same DB is **refused**:

```
DatabaseLockedError: another Argus process is already writing to argus.db
(pid=34296). Refusing to start: two writers corrupt the audit chain's seq
allocation. Stop the other process, or point this one at a different database
with ARGUS_CONFIG.
```

**What to do:** either stop the process named in the message, or — if you meant
to run something alongside a live session — use `--config argus.dev.toml`.

Why it refuses rather than sharing: the audit chain assigns its sequence numbers
from memory, so two writers both claim the same `seq`, and the loser's recorder
is dead for the rest of the process's life. Refusing to start is the cheap
failure; the alternative is a session that looks healthy and records nothing.

The lock is released by the OS when the process dies — including a hard kill —
so there is never a stale lock to clear by hand. Read-only queries are never
blocked; you can run SQL against a live session all day.

### A broken audit log halts trading

If the audit log cannot persist a record, Argus does **not** carry on:

```
CRITICAL AUDIT LOG WRITE FAILED — the black-box recorder is down; halting new entries.
CRITICAL audit log unwritable — stopping the signal loop. No further evaluation
         will occur until the process is restarted.
```

The kill switch engages, the signal loop stops, and the process must be
restarted. There is deliberately no retry: "everything is recorded before the
next action" is only a guarantee if a broken recorder stops the next action.

**What to do:** check disk space and permissions on the DB directory, confirm no
second writer, then restart. Records written before the failure are intact and
verifiable — the chain never gets corrupted, it just stops.

### A watchdog exception no longer stops the process

> **A watchdog exception no longer stops the process.** `/status`'s Watchdog row is
> the place to look: `DEGRADED` (red) means the watchdog is failing right now and
> the rows below it are as of the last clean pass; `RECOVERED` (amber) means it is
> watching again but entries stay halted until `POST /resume?confirm=yes`. Either
> way the kill switch is engaged and the HALTED row reads `NO ENTRIES — watchdog
> still running`. **Deciding whether to restart is now yours** — a transient (a
> broker blip, a one-off read failure) is already recovered and a restart buys
> nothing; a defect that repeats every tick will repeat after the restart too. The
> one thing a restart does buy is a *clean* watchdog state, which matters if a
> position's exit attempt was spent by a crash: `/status`'s Exit attempts row names
> those, with `crash_error`.

→ [a watchdog exception must not take the watchdog](decisions/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md)

### The kill switch — halt from your phone

`python -m argus` serves a small authenticated control endpoint on the address in
the loaded profile's `[server]` (`bind_host`/`bind_port`); its two
state-changing routes are `POST /kill` and `POST /resume` (the read-only status
routes share the same listener and token — see just below). It is up the whole time the process is — nights, weekends, idle
between sessions — because "reachable at any time" is the point. Trip it from any
device on the tailnet:

```bash
curl -fsS -XPOST -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
     http://<tailscale-ip>:<port>/kill
```

| Response | Meaning |
|---|---|
| `200 {"status":"tripped"}` | Halted **and** the trip is durably recorded. The `200` waits for the Tier 1 write — that's the guarantee. |
| `200 {"status":"already_tripped"}` | Safe idempotent re-hit; no second trip record. |
| `401` | Bad or missing bearer token. The attempt is logged (source IP, never the token) and recorded as `control.auth_rejected`. |
| `500 {"audit":"write_failed"}` | The switch engaged (halt stands) but the record didn't persist — investigate the DB (§9 above). |

Once tripped, the halt **persists across day-rollover AND across a restart**, and
is cleared by exactly one thing: `POST /resume` (just below). A rollover must
never un-halt (§3), and since 2026-09-01 neither may a deploy, an OOM, a reboot
or a crash-loop — the halt is read back out of the audit log at startup, so it is
state of the world rather than state of one process. The trip is a
`kill_switch.trip` audit record whose `source` says how it fired: `http_endpoint`
(this), `audit_log` (a failed write), `heartbeat` (a sustained stall), or
`reconciliation_mismatch` (a broker-vs-expected position mismatch at session
start — **sandbox/live only**; in shadow a mismatch aborts just that day without
halting the process).

A process that comes up on somebody's earlier halt says so loudly: a Slack
`kill_switch_restored` page, a `kill_switch.restored` audit record, a CRITICAL
journal line, and `ENGAGED (RESTORED)` on `/status` with the ORIGINAL press time
— never the restart's.

> **The one-tap mechanism is a phone shortcut.** Wrap that exact curl in an iOS
> **Shortcut** (a "Get Contents of URL" action: POST, one header
> `Authorization: Bearer <token>`, the URL above) or an Android **Tasker** task,
> pinned to the home screen. Tap it → the phone is already on the tailnet → the
> request fires. Store the token in the shortcut, never in a URL query string.
> Practise it now, in shadow, so the muscle memory is there before it matters.

Bind address, not token, lives in config: the token is `ARGUS_KILL_TOKEN` in
`.env`; a missing token refuses startup, and a `bind_host` that isn't up on the
host refuses to bind (§10). On the droplet the container runs host networking so
the Tailscale interface is bindable — see [§4](#the-kill-switch-endpoint-tailscale).

### Clearing a halt — `POST /resume`

The mirror of `/kill`, on the same endpoint and the same token. **Nothing else
clears a halt**: a restart, a deploy and a reboot all come back halted, which is
the point.

```bash
curl -fsS -XPOST -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
     "http://<tailscale-ip>:<port>/resume?confirm=yes"
```

`?confirm=yes` is required. This is the one control in the system that lets it
take risk again, so it is deliberately *not* one-tap — a mis-fired shortcut must
not be able to resume trading on a system somebody stopped on purpose.

| Response | Meaning |
|---|---|
| `200 {"status":"resumed","restart_required":false}` | Cleared **and** durably recorded. Entries resume within a cadence tick; no restart needed. |
| `200 {"status":"resumed","restart_required":true}` | Cleared durably, but this process *also* hit a rail (see `/status`'s HALTED row) and will not trade again regardless. Restart it now — it comes up trading. |
| `200 {"status":"not_engaged"}` | Nothing was halted. Safe idempotent no-op; no resume record. |
| `400` | Missing `?confirm=yes`. Nothing changed. |
| `401` | Bad or missing bearer token — same handling as `/kill`. |
| `500 {"audit":"write_failed"}` | The clear was **not** recorded, so it did not happen — still halted. Investigate the DB (§9). |

**Order matters when the process is also halted: assess the cause → `/resume` →
restart.** Resuming first makes it one restart instead of two. The clear is
recorded as `kill_switch.resume` with the peer IP, what it cleared, and an
optional `&reason=<text>`; it pages Slack as `kill_switch_disengaged`.

> Worth its own phone shortcut, and worth keeping it **away from the `/kill`
> one** on the home screen. Same token, different URL.

### Read-only status — glance from your phone

The same endpoint answers two **read-only** routes behind the same bearer token.
Neither changes anything, so they're safe to hit as often as you like — including
while the process idles between sessions, when there's no console to watch:

| Route | Returns |
|---|---|
| `GET /status` | JSON — machine-readable process/session state |
| `GET /` | The same data as a plain HTML page, legible on a phone |

```bash
curl -fsS -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
     http://<tailscale-ip>:<port>/status
```

One snapshot reports, roughly top to bottom:

**What is running.** `environment` + uptime; the **Build** row (`version`, stamped
into the image at build time — a `-dirty` suffix is amber, not red, because the
droplet's tracked config is edited in place); the **Config** fingerprint
`(profile, filename, sha12)`, which is the version stamp's missing half, since config
is bind-mounted and deliberately does NOT roll back with the image tag; and the
**Credentials** row naming which slot is in use (names only, never values).

**Where the day is.** The scheduler/session phase (`idle` / `logging_in` /
`reconciling` / `running` / `ended` / `aborted`), the next session open, the trading
date, and an **Event blackout** row — both how much runway `blackout_dates.txt` has
left (silent above 30 days, amber inside it, red once exhausted) and whether **today
is a blackout day**, because the blackout's only other observable is entries not
happening. **Entries** says whether they have stood down for the day.

**Whether the rails are armed.** Market-data and account **staleness ages** (the
actual age against its threshold, so drift is visible before it trips a gate — not
just a boolean); event-loop heartbeat lag; the Tier-2 audit queue depth; memory
(RSS), GC collections and stream backlogs; kill-switch and daily-loss-breaker state,
**both read from the LIVE rail** so the page cannot disagree with the gate —
`daily_loss_breaker.tripped` is `null` when no session has armed today's breaker,
which is *not* the same as `false`. Day P&L and account NLV ride alongside, with the
day's P&L flagged when it is an unanchored placeholder rather than a measurement.

**Whether anything is unmanaged.** A three-state **Watchdog** row (above) — a
watchdog that keeps running while failing is the quietest failure this system can
produce, so `NO ENTRIES — watchdog still running` is the one HALTED sentence that can
be false. Then, in order of severity: **Unwatched positions**, which get no trigger,
no mark and **no 15:30 flatten** while still consuming a concurrency slot; **Residual
orders**, which a walk left working at the broker, carrying the recheck's mode on the
summary line; **Exit triggers**, positions whose triggers cannot currently be
evaluated; and **Exit attempts**, positions whose one exit attempt is spent
(`NO EXIT LEFT`: the stop already fired and nothing automated will try again —
since 2026-09-24 a spent FILLED row also carries `STUB CLOSE: available / sent /
exhausted / disabled`, the partial's one further close —
**go and close it by hand**).

**And the tail.** Today's **raw** `shadow.would_submit` count (the un-deduped signal
pulse, *not* the scored trade-idea count `tools.mark` produces — §6), and the last
~15 audit records.

> The watchdog rows read `null` / `— (no watchdog)` in shadow, and `None` on a parked
> process. **Neither is the calm reading** — it is the same blank either way, and the
> HALTED row above is what says which.

It's the remote view of the §5 status line.

> **Wrap it in a phone shortcut too.** A second iOS **Shortcut** / Android
> **Tasker** task — same `Authorization` header, URL `.../status` (or `.../` for
> the HTML view) — pinned next to the kill button gives you a one-tap health check.
> Unlike the kill button, tapping it is harmless.

Deliberately "live status + kill switch, not a screener" (spec §5): a current
snapshot plus a recent-activity tail, not a candidate browser. The token never
appears in either response, and — being read-only — no `/status` or `/` request can
ever change system state. For deeper analysis, query the audit log directly (§6).

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `DatabaseLockedError: another Argus process is already writing` | A session is already running against that DB (§9) | Stop the named PID, or use `--config argus.dev.toml` |
| `environment='live' requires audit.synchronous to be one of ('FULL', 'EXTRA')` | Going live with shadow's durability setting | Set `synchronous = "FULL"` in `[audit]`. It costs ~50 µs/sec at Argus's write rate (§2) |
| `missing required config key: audit.synchronous` | Config predates the durability setting | Add `synchronous = "NORMAL"` (shadow/sandbox) or `"FULL"` (live) to `[audit]` |
| `AUDIT LOG WRITE FAILED` / loop stops | Audit log unwritable — disk full, permissions, or a second writer (§9) | Fix the cause, restart. Prior records are intact |
| A session recorded nothing / far fewer rows than expected | Looking at the wrong database — either the container's rather than your local one, **or the wrong chain on the volume**: `/data/argus.db` is the finished cert chain, `/data/argus-live.db` is what the live profile writes (§4) | Query the container's DB, deriving the filename from the profile: `sudo ARGUS_ENV="$PROFILE" docker compose run --rm argus python -c "import sqlite3,tomllib,os;p=os.environ['ARGUS_ENV'];db=tomllib.load(open(f'/etc/argus/argus.{p}.toml','rb'))['audit']['db_path'];print(db, sqlite3.connect(f'file:/data/{db}?mode=ro',uri=True).execute('select category,count(*) from audit_log group by 1').fetchall())"` |
| `ConfigError: no config selected` | Nothing named a config — `python -m argus` has no default path (v0.3.17) | Set `ARGUS_ENV=<profile>`, or `ARGUS_CONFIG=<path>` |
| `ConfigError: config file not found: …` inside the container | The `--config` path isn't the one mounted for this host's profile | Pass `--config /etc/argus/argus.<profile>.toml` |
| `ConfigError: config file is not readable: … IsADirectoryError` | `ARGUS_ENV` names a profile with no `argus.<profile>.toml` beside the compose file — Docker created the missing bind-mount source as a directory | Fix `/etc/argus/host.env`, or add the profile's config file |
| `required variable ARGUS_ENV is missing a value` from compose | `ARGUS_ENV` unset — by design, compose refuses rather than defaulting | `sudo ARGUS_ENV=<profile> docker compose …`; under systemd it comes from `/etc/argus/host.env` |
| `config file names profile 'live' but declares environment = 'sandbox'` | The filename and the `environment` key disagree | Rename the file or fix the key — do not pick one and hope |
| `login failed ... Client secret mismatch` | Secret + refresh token from different registrations, or cert values in live slots | `python -m argus.tools.credcheck`, re-copy both from one registration |
| `login failed ... invalid_grant` | Expired/revoked refresh token | Re-mint the refresh token |
| `User is not a TastyTrade customer` at startup, session dies | tastytrade **cert (sandbox) transiently down** — common right after the 09:30 open post-reset; recovers in ~30 min. NOT your credentials (a manual run 30 min later works). Cert-only: it says nothing about the live API | None. The supervisor's own bounded, ban-aware login retry (`[market] login_attempts` / `login_retry_interval_s`, 6 × 10 min ≈ 50 min) rides this out without a systemd restart and never crashes the process. If cert stays down past the window the day is missed — check `session.login_gave_up`. **Do not shorten the interval**: each failed OAuth refresh accrues toward tastytrade's undocumented IP-block threshold |
| `could not fetch accounts after N attempts` | Broker REST slow/failing *after* a successful login — cert is slow on a first call from a new host | Retry; if attempts show timeouts, raise `broker.http_timeout_s` in the profile config |
| `missing required config key: broker.http_timeout_s` | Config predates the explicit HTTP timeout | Add `[broker] http_timeout_s = 30.0` |
| `missing or malformed [reconcile] section` | Config predates the reconcile retry (v0.3.11) — **this config must deploy WITH the image** | Add the `[reconcile]` section (§ above); copy the three keys from `argus.sandbox.toml` |
| `reconcile.retry_budget_s must be >= reconcile.retry_interval_s` | A budget shorter than one interval means exactly one attempt, spelled like a retry loop | Raise the budget, or drop the interval |
| `missing required config key: server.bind_host` / `server` section | Config predates the kill endpoint | Add a `[server]` section with `bind_host` (Tailscale IP) + `bind_port` (§4) |
| `MissingKillTokenError: missing required env var ARGUS_KILL_TOKEN` | No kill-switch token in the environment | Set `ARGUS_KILL_TOKEN` in `.env` (§1) — the endpoint must be authenticated, so the process refuses to boot without it |
| `ControlServerBindError: could not bind ... control server` | `[server] bind_host` isn't up on the host — Tailscale down, wrong IP, or (in Docker) not host networking | Bring Tailscale up; set `bind_host` to `tailscale ip -4`; in Docker confirm `network_mode: host` (§4) |
| `POST /kill` returns `401` | Bad/missing bearer token | Send `Authorization: Bearer $ARGUS_KILL_TOKEN`; confirm it matches `.env`. The attempt is logged (IP only) and recorded as `control.auth_rejected` |
| `POST /kill` connection refused | Not on the tailnet, wrong `bind_host`/port, or the process is down | Join the tailnet; check `[server]` matches `tailscale ip -4` + port; confirm the service is up (§4) |
| Process starts and takes no entries; `/status` shows `ENGAGED (RESTORED)` | **Working as designed** — an earlier `/kill` (or a rail trip) was never resumed, and a restart does not clear one | Assess the cause, then `POST /resume?confirm=yes`. `kill_switch.restored` and the trip's `source` say where the halt came from |
| `POST /resume` returns `400` | Missing the confirmation | Use `/resume?confirm=yes` — the resume route is deliberately not one-tap |
| `POST /resume` returns `200 restart_required: true` | The clear is durable, but this process also hit a rail and will not trade again | Restart the service now; it comes up trading. Do NOT resume again after the restart |
| `POST /resume` returns `500 write_failed` | The clear could not be recorded, so it did not happen | Fix the audit DB (§9) and retry — the halt is still in force, which is the safe direction |
| Reconciliation mismatch (won't resume) | Broker holds a position Argus didn't record | **Shadow:** that day is skipped and the process keeps running — investigate before the next session. **Sandbox/live:** it halts the whole process (kill switch + stop, `source=reconciliation_mismatch`); a restart re-reconciles and only resumes once it's clean. Flatten the account, or investigate the real discrepancy — don't override blindly |
| `no 0DTE expiry today` | Weekend/holiday, or wrong root | 0DTE is under `SPXW`, not `SPX`. Run `argus.tools.chain` to confirm. |
| `greeks 0/120` while quotes flow | Options (OPRA/greeks) entitlement missing | Sort entitlements on the tastytrade account; index data alone isn't enough |
| `signal.none` with `no_short_leg_with_fresh_delta` | The greeks feed is behind `staleness.greeks_max_age_s`. NOT an entitlement gap and NOT an empty chain — the ladder is fully priceable, there is just no delta to pick a short strike by | Read `min_greeks_age_s` on the record and `g_age=` on the 15s status line: that is the FEED, and it keeps reporting after `short=` has collapsed to zero. The greeks feed refreshes the whole chain in bursts (median 137s / p90 421s, measured 2026-08-27), so the question is whether the bound clears the burst gap. **Never raise `market_data_max_age_s` for this** — it governs entry pricing, exit pricing and the gate's staleness rail |
| Only `signal.none`, never a candidate | `min_credit` too high, or window closed | Check near-misses (§6); lower `min_credit` or widen `entry_*`. Rule out the greeks row above first — it was 97% of them on 2026-08-27 |
| `acct_age` climbing unbounded | Account link dead (not just quiet) | Restart; the liveness refresh should hold it under `account_refresh_s` |
| `account liveness refresh failed` (WARN) | Broker blip — 502/timeout from the cert API | None; the loop retries at 1s/2s/4s… and logs `account.refresh_recovered` |
| `dxlink link lost ...; reconnecting` (WARN) | Websocket dropped (network blip, laptop sleep, server-side) | None; it reconnects and resubscribes. `reconnects=` appears on the status line |
| `reconnects=` climbing steadily | Flaky link, not a one-off | Check wifi/VPN. Every gap is a window where entries are blocked |
| `[MD LINK DOWN]` persisting | Reconnect failing repeatedly | Check `marketdata.disconnected` payloads for the error type |
| `... entries are BLOCKED` (ERROR) | Refresh failing long enough that `acct_age` passed the tolerance | Query `account.refresh_failed` for the cause; gate denials in this window are correct, not a bug |
| `md_age` climbing | Market-data feed stalled | Restart; check connectivity |

---

## 11. Good to know

- **`environment` is what decides whether orders are real** — not the command, not
  the data feed, not this README. Read it off `/status`, which carries both it and
  the config fingerprint. Since 2026-09-15 the droplet's says `live`.
- **Shadow mode submits nothing**, and an `order.event` in the audit log during a
  shadow run is unexpected and worth investigating — it means something placed an
  order that Argus didn't. In `sandbox`/`live` that category is ordinary.
- **The traded account is DEDICATED to Argus, and that is a premise of four rails
  rather than a tidiness preference.** Reconciliation compares the broker's entire
  position map against a table only Argus writes; the concurrency cap counts the
  account's legs; the daily-loss breaker measures whole-account NLV delta; and the
  relative size rail is a percentage of whole-account NLV. One foreign position makes
  all four wrong at once. **No cash may move into or out of the account on a trading
  day** either — `DailyPnl` reads a transfer as P&L, and a same-day restart re-uses
  the day's original anchor, so the contamination is not restart-clearable.
- **Sandbox resets every 24h** — positions/trades wiped nightly; the account
  itself persists. Don't expect sandbox positions to survive a day. **`argus-live.db`
  is never reset**: the wipe keys on `environment == "sandbox"`, so every row in its
  `positions` table is a fact about a real account.
- **The audit log grows** — roughly 7 MB per full session (~1.8 GB/year at a 5s
  cadence). SQLite handles that without breaking a sweat: write latency is flat
  from megabytes to multiple GB, because the log is append-only against a
  monotonic integer key. Size is not something you need to manage for
  performance.
- **To start a fresh dataset, archive — don't delete.** Renaming preserves a
  verifiable chain you can still query later:
  ```bash
  mkdir -p archive && mv argus.db archive/argus.db.$(date +%F)
  mv argus.db-wal argus.db-shm archive/ 2>/dev/null   # if present
  ```
  Stop any running session first (the writer lock will refuse a restart
  otherwise). The next run creates a clean DB starting at `seq` 1. `archive/` is
  gitignored. **This is a LOCAL/dev habit and never something to do to
  `argus-live.db`** — that chain is the trading record.
- **Backups: nightly snapshots on the droplet, none locally.** On the droplet,
  `argus-snapshot.timer` writes a verified off-volume snapshot each night, pulled
  to your NAS (§4). On your **Mac**, the local `argus.db` has no automated backup
  — copy it read-only (`sqlite3 'file:argus.db?mode=ro' .backup …`) if a dev
  session's data matters. Continuous replication (Litestream → R2) was scoped as the
  before-live upgrade and **did not ship before the flip** — designed, not built, so
  live runs on the volume plus nightly snapshots.
- **The kill switch is reachable from your phone.** `python -m argus` (and a
  `tools.shadow` run) serves a small always-on `POST /kill` endpoint, bound to the
  droplet's Tailscale interface and authenticated with a bearer token
  (`ARGUS_KILL_TOKEN`). One authenticated request halts new entries, and it does
  not return `200` until that trip is durably recorded (§9). A failed audit write
  *also* engages the switch automatically (§9), as does — if enabled — a sustained
  event-loop stall. **On the live profile this halts real entries and chases a
  working entry order**, measured at about 4–5 seconds from press to cancel; the
  one-tap phone shortcut is the mechanism that makes that number mean anything, so
  have it wired and practised. A trip is **durable** — it survives the rollover, a
  deploy, an OOM and a reboot — and only `POST /resume?confirm=yes` clears it. The
  daily-loss breaker is the other automatic rail (trips on `daily_max_loss`).
  See §9 for how to trigger and verify the endpoint.
- **The same endpoint serves a read-only status view.** `GET /status` (JSON) and
  `GET /` (a phone-legible HTML page), behind the same token — a safe-to-hit-anytime
  snapshot of the build, the config fingerprint, phase, staleness ages, heartbeat,
  kill/breaker state, open positions in both units, the four **unmanaged/watchdog
  rows**, and the recent audit tail. It's how you check on the process from your
  phone without tailing logs; it changes nothing (§9).
- **Read the four watchdog rows, not just "open positions".** In severity order:
  **Unwatched positions** — Argus holds it and cannot attribute it, so it gets no
  trigger, no mark and **no 15:30 flatten** while still consuming a concurrency slot;
  **Residual orders** — a walk left an order working at the broker; **Exit triggers**
  — whether what is held can actually be evaluated (`all evaluable` is the steady
  state, `degraded (credit_multiple) for 3m 20s` is a stale wing quote with the
  underlying backstop running on and is NOT a page, `BLIND` means nothing is watching
  and does page); and **Exit attempts** — a position whose one attempt is spent, which
  is yours to close by hand. `— (no watchdog)` is shadow, where nothing is held.
  **The duration is the signal**: seconds is a quote gap, twenty minutes is worth
  looking at.
- **Don't trust a single boundary fill.** On delayed data especially, a credit
  landing exactly on `min_credit` is mechanism validation, not evidence of edge.
  Judge edge from distributions across many sessions of *live* data.
