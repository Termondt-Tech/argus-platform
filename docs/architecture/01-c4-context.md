# C4 Level 1 — System Context

**Audience:** traders, compliance, executives. No code knowledge assumed.

Argus is a single always-on system that trades 0DTE SPX credit spreads on **one dedicated
tastytrade account**, live since 2026-09-15 at minimum size, with no human in the loop for
entries or exits. This diagram deliberately shows Argus as one opaque box: the only things
worth knowing at this level are *who* can act on it, *what* it depends on, and *where* a
human is told something. The human interactions that matter operationally are two: the
**kill switch** (stop taking new risk, one tap from a phone) and its only counterpart,
**resume** (a deliberate, confirmed request to allow entries again).

Three details on this diagram are load-bearing rather than internal. First, Argus holds
**two separate tastytrade sessions** on two app registrations: a trading session whose
world (cert or production) follows the configured `environment`, and a *read-only*
market-data session on production quotes (`broker/session.py`, `SessionRole`). Second, the
traded account is **dedicated** to Argus — every account-level number it reads (positions,
net liquidation value, day P&L) is treated as Argus-only, so a foreign position would make
reconciliation halt the process. Third, every human-facing surface is bound to a private
Tailscale address and requires a bearer token; nothing Argus serves is on the public
internet.

```mermaid
%%{init: {"c4": {"useMaxWidth": false}}}%%
C4Context
    title System Context — Argus 0DTE SPX execution system (live since 2026-09-15)

    Person(operator, "Rich — operator", "Sole operator. Phone Shortcuts to kill, resume and read status. Does not place trades by hand, except the documented manual-close SOP.")

    System(argus, "Argus", "Fully automated 0DTE SPX credit-spread execution. One trading session per market day, unattended: selects candidates, risk-gates them, submits and price-walks one multi-leg order, watches the open position with a virtual stop (the underlying reaching the short strike, since v0.5.2) and a 15:30 flatten (deferrable since v0.5.2 on a calibrated far-OTM test), and records everything to a hash-chained audit log.")

    System_Ext(tt_trading, "tastytrade — trading API", "Order routing, positions, balances and the order-status stream for ONE dedicated account. Cert or production, selected by environment.")
    System_Ext(tt_data, "tastytrade — market data", "Real-time SPX and SPXW quotes and greeks over DXLink, plus the 0DTE option chain. Production, read-only, separate app registration.")
    System_Ext(slack, "Slack", "Two incoming webhooks. Alerts channel: halts, reconciliation mismatches, stops and exits, unmanaged positions. Optional report channel: the end-of-day digest.")
    System_Ext(tailnet, "Tailscale private network", "The only network path to Argus's control surface. No public exposure.")
    System_Ext(hostmon, "DigitalOcean monitoring", "Host health alerts — CPU, memory, disk, load — to email and Slack. Configured outside this repo.")
    System_Ext(nas, "Off-box backup (NAS)", "Pulls nightly verified audit-DB snapshots and the EOD report files. The NAS initiates; Argus holds no NAS credentials.")
    System_Ext(gsheets, "Google Sheets", "The trading ledger dashboard, reconciled against the broker to the cent every night. Never gates trading.")

    Rel(operator, tailnet, "Kill, resume, status check", "iOS Shortcuts, HTTP over tailnet")
    Rel(tailnet, argus, "POST /kill, POST /resume?confirm=yes, GET /status, GET /", "HTTP + bearer token")

    Rel(argus, tt_trading, "Submits, replaces and cancels multi-leg limit orders. Reads positions, balances and live orders. Consumes fill events.", "REST + AlertStreamer WebSocket, OAuth")
    Rel(argus, tt_data, "Subscribes to quotes and greeks, loads the 0DTE chain", "DXLink WebSocket + REST, OAuth, read-only")
    Rel(argus, slack, "Alerts a human, and posts the daily report", "HTTPS webhook POST")
    Rel(argus, gsheets, "Nightly ledger push, read-only reconciliation against the broker", "HTTPS, service-account JWT, from the EOD host job only")

    Rel(hostmon, operator, "Host alerts", "Email, Slack")
    Rel(nas, argus, "Pulls snapshots and reports", "SSH, NAS-initiated")

    UpdateRelStyle(operator, tailnet, $offsetY="-30")
    UpdateRelStyle(tailnet, argus, $offsetY="-10")
    UpdateRelStyle(argus, tt_trading, $offsetX="-90", $offsetY="20")
    UpdateRelStyle(argus, tt_data, $offsetX="10", $offsetY="30")
    UpdateRelStyle(argus, slack, $offsetX="-40", $offsetY="10")
```

## What each external dependency is used for

| External system | Source of truth | Notes |
|---|---|---|
| tastytrade trading API | `broker/session.py`, `execution/broker.py`, `account/rest.py`, `account/stream.py`, `account/refresher.py` | **The only source of truth for positions and orders.** Argus refuses to resume on any mismatch with its own records (`reconcile.py`). Credentials are `TASTYTRADE_TRADING_CERT_*` / `TASTYTRADE_TRADING_LIVE_*`; which slot is used follows `environment`, and a `live` profile refuses to start without its own slot or with one holding the cert values. The account is pinned by `[broker] account_number`, which `config.py` requires under `live`. |
| tastytrade market data | `marketdata/dxlink.py`, `signal/live_chain.py` | `TASTYTRADE_MARKETDATA_LIVE_*`, its own registration. `TastytradeSession.trading_session` raises if asked for a market-data session, so nothing that can move money can hold the production-quote one. |
| Slack | `notify.py` (alerts), `scripts/argus-eod.sh` (report) | Optional. A missing `ARGUS_SLACK_WEBHOOK_URL` degrades alerting to the log and never refuses startup. `ARGUS_REPORT_SLACK_WEBHOOK_URL` routes the EOD digest to its own channel and falls back to the alerts webhook. |
| Tailscale | `config.py` `[server] bind_host`, `docker-compose.yml` `network_mode: host` | Argus binds exactly one listening socket, on the tailnet address. A wildcard bind is a `ConfigError`. |
| DigitalOcean monitoring | `CLAUDE.md` (deployment section) | > **Unverified from source:** configured in the DO control panel; no code or config in this repo defines these alert policies. |
| NAS backup | `scripts/argus-snapshot.sh`, `scripts/argus-pull*.sh`, `argus-snapshot.timer`, `argus-pull-check.timer` | The whole off-box durability story — Litestream is deferred past go-live by decision. `argus-pull-check` watches both ends of the pull and pages if either goes stale. |
| Google Sheets | `tools/ledger.py`, `tools/broker_ledger.py`, `tools/sheets.py`, `tools/rs256.py`, `scripts/argus-eod.sh` | Since v0.4.7 (2026-09-22). Service-account auth (OAuth was tried and abandoned the same day — an External app cannot leave "Testing" without a domain and a privacy policy). Read-only against the audit DB, runs in its own throwaway container as the EOD job's last two steps. **Can never gate trading** — an AST test pins that it cannot reach the submission path. |

## What is deliberately *not* on this diagram

- **No broker-side stop orders.** Argus never sends a stop-market order, and never uses the
  broker's complex-order API (OCO/OTO). Exits are a virtual stop evaluated in-process and
  routed as a walked limit order, escalating at most to a single market-crossing **limit**
  capped at the spread width (`execution/watchdog.py`).
- **No web UI.** The operator surface is a JSON endpoint and a single dependency-free HTML
  page (`status.py`), reachable only over the tailnet.
- **No second Argus.** Exactly one process may write a given audit database; a second one
  refuses to start (`db/lock.py`).
- **No shared account.** Cash moving in or out of the traded account during a trading day is
  read as P&L by the daily-loss breaker, and any position Argus did not place hard-halts the
  first reconcile. Both are operating rules, stated in `CLAUDE.md`, not code paths.
