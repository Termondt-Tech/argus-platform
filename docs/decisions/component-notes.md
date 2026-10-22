# Component notes — the annotated file map

*Per-file design notes: what each module owns and the traps it exists to prevent.*

> Relocated from `CLAUDE.md` on 2026-08-12, verbatim. `CLAUDE.md` keeps a condensed
> version of this tree; this is the full annotated form. Several notes here are the
> ONLY record of a specific hazard — notably the SDK streamer task-ownership rule in
> `marketdata/dxlink.py` and the hardened liveness refresh in `account/stream.py`.
> See [`README.md`](README.md) for the index.

```
src/argus/
  __main__.py            # `python -m argus` entrypoint (pins SDK loggers to WARNING
                         # so ARGUS_LOG_LEVEL=DEBUG can't leak a bearer token)
  app.py                 # Application supervisor: process-lifetime resources +
                         # SessionScheduler; builds FRESH per-day risk state
                         # (breaker/day-P&L/gate) each session; run_single_session;
                         # _halt_process = the ONE engage-kill-switch + request_stop
                         # pairing (audit-failure AND reconcile-mismatch halts reuse it)
  scheduler.py           # SessionScheduler: pure plan() + async driver; one session
                         # per trading day, idle between, interruptible on stop
  marketcalendar.py      # MarketCalendar: trading-day / holiday / half-day / window
                         # (ET); pure scheduling, distinct from macro blackout days
  session.py             # run_session: THE pipeline body (login-retry -> reconcile ->
                         # chain -> loop -> terminal action). `run_shadow_session` is
                         # a kept alias. SessionState; SessionAborted; per-session
                         # reconcile mismatch is env-gated (shadow aborts the day;
                         # sandbox/live halt via injected `halt` seam)
                         # + TradingSide/_build_trading_side (chunk 5): the entry
                         # executor, watchdog and fill consumer, built together or not
                         # at all — the ONLY thing `environment` changes
                         # + startup_reconcile: the short-lived process-start trading
                         # session sandbox/live reconcile through (shadow still skips)
  control.py             # ControlServer: always-on authenticated POST /kill +
                         # read-only GET /status (JSON) & GET / (HTML) on ONE listener
                         # (spec §6/§3/§5). stdlib asyncio.start_server (NOT a web
                         # framework — see Build/test); Tailscale-only bind, bearer
                         # token from ARGUS_KILL_TOKEN, constant-time compare, waits
                         # for the Tier 1 trip write before 200, idempotent,
                         # source=http_endpoint. GET routes are purely additive.
  status.py              # StatusService.assemble -> StatusSnapshot (ONE assembly,
                         # to_json_dict + to_html); SessionStatus (process-lifetime
                         # session-phase/live-risk publisher the session body writes)
                         # + LinkHealth: connected/reconnects/consecutive refresh
                         # failures for both streams, SESSION-scoped and read live
                         # off the adapters. Reconnect counts are muted (recovered,
                         # not a fault); red is down-now or refresh failing
                         # + read_rss_bytes/gc_snapshot: the lag-ramp instruments.
                         # /proc/self/statm + O(1) gc stats, both fail-soft. NEVER
                         # gc.get_objects() (AST-tested) — materializing the object
                         # graph inside the stall diagnostic would BE a stall
  config.py              # fail-closed config loader (COA timing, risk caps, staleness,
                         # heartbeat, [market] calendar + login retry, [server] bind
                         # addr, [watchdog] trigger levels + debounce + exit walk)
  alerts.py              # AlertFn seam (default logs; notify.py is the real path)
  notify.py              # SlackNotifier: THE phone-reachable AlertFn (chunk 4).
                         # Bounded queue + to_thread POST (never blocks the loop),
                         # never raises, webhook from ARGUS_SLACK_WEBHOOK_URL and
                         # never logged in full. build_alert_fn() = log + Slack.
                         # Top-level, not under execution/ — app/heartbeat/audit
                         # are callers too, so execution/ would invert the layering
                         # + PER-KIND COALESCING (2026-08-05): first of a kind always
                         # delivered, repeats inside throttle_s counted and summarized
                         # onto the next one. Throttled at ENQUEUE so a storm of one
                         # kind can't push another out of a full queue
  heartbeat.py           # LoopHeartbeat: event-loop-lag early warning (§4, §9).
                         # Recovery is LOGGED, never alerted — its anti-spam assumes
                         # long rare stalls; the droplet's are short and periodic
  orchestrator.py        # SignalLoop: signal->gate->(would-submit | SubmitFn);
                         # size_contracts applies ALL THREE size rails, tightest wins
  reconcile.py           # broker reconciliation: pure compare, PositionStore,
                         # Reconciler; mismatch_halts_process(env) — the ONE
                         # shadow-vs-rest boundary both reconcile call sites gate on.
                         # + reconcile_orders: unaccounted WORKING/FILLED broker
                         # orders, in the SAME result so they inherit that halt
                         # + reconcile_and_record: THE one sequence both call sites run
                         # (reset check -> compare -> stamped record -> alert), and
                         # sandbox_reset_decision / last_clean_reconciliation, the
                         # day-rollover answer to the sandbox 24h account wipe
  clock.py               # ET exchange-time helpers (trading date/time — never host-local)
  staleness.py           # THE fail-closed staleness rule (one definition, used everywhere)
                         # NB: account state needs a LIVENESS FLOOR (staleness.account_refresh_s)
                         # — the AlertStreamer pushes only on change, so a quiet/flat
                         # account looks "stale" and would block every entry. The floor
                         # must also survive a flaky broker: see account/stream.py.
  signal/
    models.py            # OptionQuote, ChainSnapshot, SpreadSignal (mid DELEGATES to
                         # execution/zerobid.py; carries captured_at for the
                         # execution-layer freshness bound and underlying_price —
                         # the arrival price the entry drift breaker measures against;
                         # NaN = not recorded, and it serializes as None, never NaN)
    chain.py             # ChainSource port + FakeChainSource
    contracts.py         # OptionContract + pure 0DTE selection / strike-window filter
    live_chain.py        # DXLinkChainSource: instruments API + Quote/Greeks join
    strategy.py          # Strategy protocol + CreditSpreadStrategy (baseline, config-driven)
                         # + RegimeSnapshot / classify_iv_regime / _side_permitted /
                         # size_multiplier — ALL INERT while config.regime is None
                         # + THE EVENT BLACKOUT, which is LIVE and deliberately does
                         # NOT consult regime_config (no separate "arm it" step)
    regime.py            # compute_regime: PURE VIX-style integrated-variance estimate
                         # over the 0DTE OTM strip (prices, not an IV field — there
                         # isn't one). TELEMETRY ONLY today. NEVER returns None and
                         # never raises: a failed estimate is NaN but still carries
                         # `today`, which is what the blackout reads. Carries
                         # minutes_to_settlement because the reading is biased 1.03x
                         # at 10:00 -> 1.68x at 15:45 — condition on it, don't pool
  db/
    connection.py        # WAL + config-driven `synchronous` (live must be FULL)
    lock.py              # DatabaseLock: exclusive flock — ONE writer process, enforced
    migrations.py        # numbered, immutable, atomic migration runner
    migrations/*.sql     # 0001 audit_log, 0002 positions (reconciliation target),
                         # 0003 (category,ts) index — bounds /status would_submit COUNT,
                         # 0004 orders + order_legs (the order ledger; applied_quantity
                         # is the fill-idempotency record)
  audit/
    models.py            # AuditRecord + canonical hashing (sha256 chain)
    log.py               # AuditLog: Tier 1 awaited (to_thread), Tier 2 bounded queue.
                         # read(fn)/write(fn) = THE gateway to the process's single
                         # writer connection for the non-audit tables (never a 2nd handle)
                         # + counts: in-memory per-category tally (seeded once at
                         # construction, incremented on _advance). The status line's
                         # O(1) replacement for a 15s full scan on the event loop
  risk/
    killswitch.py        # KillSwitch: immediate halt + Slack alert + Tier 1 trip record
                         # (source + optional `detail` dict, e.g. reconcile discrepancies).
                         # The alert lives HERE, in the one funnel every trip source
                         # passes through. Owns NO stop event: it halts new risk, never
                         # the process — see "Kill-switch scope"
    breaker.py           # DailyLossBreaker: latching daily max-loss circuit breaker
    gate.py              # PreTradeGate: fail-closed one-time pre-order check (Tier 1)
    models.py            # EntryProposal, RiskSnapshot, GateDecision
    snapshot.py          # RiskSnapshotBuilder + DailyPnl (NLV-anchored total day P&L)
  marketdata/
    port.py              # MarketDataPort Protocol (the seam consumers depend on)
    models.py            # Quote (domain type, decoupled from dxfeed)
    book.py              # QuoteBook: shared quote state + fail-closed staleness
    dxlink.py            # DXLinkMarketData: real adapter (_ingest unit-tested)
                         # + ONE task owns the streamer via `async with` and reconnects
                         # with backoff/resubscribe. NEVER drive the SDK streamers with
                         # __aenter__/__aexit__ across start()/stop(): they yield from
                         # inside an anyio task group whose cancel scope must be exited
                         # by the entering task, and a real socket failure then unwinds
                         # from the wrong task and kills the process (seen 2026-07-21).
    fake.py              # FakeMarketData: deterministic, clock-controlled (sim seed)
  account/
    port.py              # AccountStatePort + BrokerAccountPort protocols
    models.py            # Position, Balances, AccountSnapshot + SDK mapping (pure)
    rest.py              # BrokerAccount: authoritative positions/balances (REST)
    cache.py             # AccountStateCache: live state + fail-closed staleness
    stream.py            # AlertStreamerAccount: listens for positions/balance/PlacedOrder
                         # + hardened liveness refresh: per-attempt timeout, 1s/2s/4s
                         # backoff (NOT a full interval per failure), re-auth on
                         # token_invalid, `account.refresh_failed`/`_recovered` telemetry
                         # + same one-task streamer ownership + reconnect as dxlink.py.
                         # The REST refresh deliberately survives a stream reconnect —
                         # it's what keeps account state usable while the stream is down.
    fake.py              # FakeAccountState, FakeBrokerAccount (tests + sim seed)
  broker/
    session.py           # TastytradeSession: OAuth wrapper, sandbox-first at boundary
  execution/             # chunk 1 PRIMITIVES ONLY — no loop, cadence, retry or wiring
    __init__.py          # re-exports NOTHING on purpose (signal.models imports zerobid,
                         # build imports signal.models — a re-export would cycle)
    zerobid.py           # THE zero-bid floor + Decimal tick rounding (one definition;
                         # signal/models.py and chunk 3's repricing both use it)
    models.py            # Order/OrderLeg/OrderAck + OrderStatus & LegAction MIRRORING
                         # the SDK enums (drift-tested); partial fills by quantity;
                         # duck-typed map_placed_order (no tastytrade import).
                         # + classify_order (chunk 2) and CONFIRMED_IN_MARKET
                         # (chunk 3) = {LIVE} only — RECEIVED is MEASURED not to
                         # mean in-the-market; widening the set is a measurement
    build.py             # PURE (SpreadSignal, direction) -> unsent Order. Reuses the
                         # gate-approved floored mid, bounds signal age, one 2-leg order
    broker.py            # OrderExecutionPort (4 separate async calls) +
                         # TastytradeOrderExecution over place/replace/delete/get_live.
                         # PLAIN LimitOrder — never the complex-order API (spec §6)
    fake.py              # FakeOrderExecution: deterministic; encodes the replace-links
                         # assumption that tools/order_smoke.py verifies for real
    # -- chunk 2: recording, fills, duplicate guard (wired by chunk 5)
    store.py             # SqliteOrderStore: the orders/order_legs ledger + the ONLY
                         # writer of `positions`. Idempotent on cumulative
                         # remaining_quantity; SQL runs through AuditLog.write
                         # + open_spreads() (chunk 4): reassembles held SPREADS from
                         # the ledger — the only place the two legs are grouped and
                         # the only source of entry credit (`positions` has none)
                         # + ledger_state()/clear_ledger() (chunk 5): the counts and ET
                         # trading date the sandbox-reset decision reads, and the
                         # one-transaction wipe it performs
    fills.py             # FillConsumer: plugs into AlertStreamerAccount's EXISTING
                         # on_order hook; books deltas, never adopts a foreign fill
    guard.py             # DuplicateOrderGuard: the only backstop for spec §9.
                         # Asks the broker; raises rather than allowing on failure
    audited.py           # AuditedOrderExecution: same port, Tier 1 write BEFORE the
                         # wire + order recording. Decorator — chunk 1 is untouched.
                         # `records_orders` is what chunk 3 refuses to run without
    # -- chunk 3: the price-walking loop (still unwired into SignalLoop)
    loop.py              # + the optional injected halt_check (HaltCheckFn): THE seam the
                         # kill switch reaches an in-flight order through. Armed by
                         # entry.py, NEVER by watchdog.py (an AST test pins that).
                         # _stop_reason() returns the REASON so a kill and a session
                         # close are distinguishable in walk.finished
                         # + WalkOutcome.DRIFTED + the optional injected drift_check
                         # (follow-on): a circuit breaker at the cycle boundary for
                         # the ladder's stale reference. Still NO quote path here.
                         # PriceWalkingLoop: THE cancel/replace state machine, ONE
                         # implementation for entry AND (chunk 4) exit, parameterized
                         # by WalkPolicy. Cycle clock starts on CONFIRMED_IN_MARKET,
                         # never on the ack; a cycle is >= the full COA settle window;
                         # replace() reprices, cancel() only abandons; disconnects are
                         # recovered by re-querying the broker, NEVER by re-sending.
                         # + PriceWalk (pure ladder), WalkOutcome/WalkResult, OrderView
    ratelimit.py         # OrderRateLimiter + ThrottledOrderExecution: process-wide
                         # floor under /orders request rate. Insurance against a
                         # runaway, not a cadence input (40 req/s confirmed)
    # -- chunk 4: the position watchdog + exit handoff (wired by chunk 5)
    watchdog.py          # PositionWatchdog: spec §4's separate always-watching task.
                         # Two triggers (2x-credit mid, underlying-vs-short-strike),
                         # debounced by SustainTracker; halts the PROCESS on any
                         # unhandled exception (stricter than SignalLoop, signed off).
                         # + LegQuotes = THE OCC->streamer resolution (they are
                         # different symbols; getting it wrong = a stop that never
                         # fires, silently) + ExitExecutor, which owns the
                         # WALKED_AWAY escalation and the market-crossing price —
                         # exit business logic that chunk 3 deliberately never learns
    # -- chunk 5: the wiring. THE only environment-gated submission path
    pipeline.py          # build_execution_stack: THE assembly — audit OUTERMOST ->
                         # throttle -> tastytrade (a test pins the nesting) — plus the
                         # store/guard/limiter built on it. ONE stack per process, so
                         # the rate limiter is genuinely process-wide.
                         # + trades_for_real(config) = the ONE submission boundary
    entry.py             # EntryExecutor: the SubmitFn seam SignalLoop has had since
                         # shadow mode. Refuses on an engaged kill switch (closing the
                         # gate/submit race) and arms the walk's halt_check — the entry
                         # side of the kill switch's blast radius.
                         # Guard ONCE (here, in the soft region) -> build
                         # -> PriceWalkingLoop -> handle all 8 outcomes explicitly.
                         # Owns the exception boundary: SOFT (end the day, keep the
                         # process) above the walk, HARD (kill switch + stop) from the
                         # first submit onward — one call boundary, not a condition.
                         # + the dry-run activation mode, which stops BEFORE the walk
                         # (a dry run mints no order id, so there is nothing to walk)
                         # + one entry attempt per candidate per process
                         # + UnderlyingDriftCheck: the breaker's implementation, over
                         # chunk 4's LegQuotes.spot passed in by session.py (no import
                         # of watchdog.py, no second quote path)
  shadow/
    outcome.py           # settlement math (SPXW = PM-settled at the close) + the
                         # DEDUP that turns N re-proposals into distinct trade ideas
    marker.py            # read shadow.would_submit back, score it, record shadow.outcome
  tools/
    mark.py              # score a session against settlement (--from-feed | --settlement).
                         # Price columns render through Decimal at 3dp, NOT `.2f` —
                         # a credit is a HALF-TICK ($0.025) quantity, so 2dp both
                         # broke ties on float noise and could not reconcile against
                         # its own P&L. See "The mark tool's credit column"
    blackout_dates.py    # regenerate the macro-event blackout file: FRED for
                         # CPI/NFP/PCE (ARGUS_FRED_API_KEY — Argus's OWN, never
                         # TOMIC's), hand-maintained fomc_dates.txt for FOMC.
                         # stdlib urllib, not requests. --check writes nothing.
                         # Operational only — `python -m argus` never reads the key
    quotes.py            # manual sandbox market-data smoke tool
    account.py           # manual sandbox account + reconciliation smoke tool
    chain.py             # option-chain diagnostic: expirations/roots/strikes actually available
    order_smoke.py       # manual sandbox order-primitive validation: dry-run by
                         # default; --live-submit does one real submit/replace/cancel
                         # priced away from the market. Refuses environment="live"
    shadow.py            # THIN wrapper: one time-boxed session via
                         # Application.run_single_session (dev runs; submits nothing)
tests/                   # pytest, one module per component
argus.toml               # sample runtime config (live data-collection sessions)
argus.dev.toml           # dev/tool/experiment config — writes to gitignored dev/
blackout_dates.txt       # macro-event entry blackout ([events] points here).
                         # GENERATED + FRED-verified 2026-08-04 (18 dates); ENDS
                         # 2026-12-23 — regenerate when 2027 calendars publish.
                         # Bind-mounted beside argus.toml; must deploy WITH it
fomc_dates.txt           # hand-maintained FOMC decision days (no FRED release id
                         # exists); merged into blackout_dates.txt by the generator
Dockerfile               # multi-stage image; digest-pinned base, non-root, /data volume
docker-compose.yml       # the one service: env_file, named volume, restart policy
argus.service            # THE always-on unit: python -m argus w/ loop (enable this)
argus-shadow.timer       # RETIRED — superseded by argus.service; do NOT enable
argus-shadow.service     # manual-only time-boxed tools.shadow run (not timer-driven)
argus-snapshot.{service,timer}  # nightly consistent+verified DB snapshot off-volume
scripts/argus-snapshot.sh       # the snapshot (SQLite online backup + verify_chain + rotate)
scripts/pull-snapshots.example.sh  # NAS-side pull (NAS initiates; droplet holds no NAS creds)
requirements.txt         # HASH-PINNED runtime lock (generated; see its header)
.dockerignore            # keeps .env / *.db / .venv out of the build context
```
