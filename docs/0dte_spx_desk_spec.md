# 0DTE SPX desk — design spec

**Status:** Design phase — nothing built yet
**Companion to:** TOMIC `CLAUDE.md` (this is a fully separate app/repo/deploy, not a TOMIC tab)
**Last updated:** 2026-07-17

---

## 1. Problem & context

TOMIC's other desks are decision-support: a human reviews screener output and clicks through to place a trade. 0DTE SPX credit spreads run on a different clock — minutes matter, not end-of-day — and the roadmap already flags this as a purpose-built system, not a screener tab.

This app is **fully automated**: no per-trade human confirmation. That single decision is what separates this build from everything else TOMIC has done — order execution, real-time risk management, and crash recovery move from "future work behind a policy gate" to the core of the system.

## 2. Goals (V1)

- Automatically identify, size, enter, and exit SPX/SPXW 0DTE credit spreads within defined risk parameters
- Enforce hard risk limits without human intervention (position size, daily loss, concurrent positions)
- Survive a mid-session crash without losing track of live positions or duplicating orders
- Produce an immutable, queryable record of every decision and order for post-hoc evaluation
- Be stoppable in one action, at any time, including remotely

## 3. Non-goals (V1)

- Multiple underlyings — SPX/SPXW only. No /ES, no single-name 0DTE.
- Naked or ratio structures — defined-risk verticals only.
- A full AG-Grid style screener UI — this app's UI is a monitoring surface, not a candidate browser.
- Historical backtesting tooling — shadow mode against live data serves that purpose for V1.
- Shared deploy or shared credentials with TOMIC.

## 4. Architecture overview

Single persistent `asyncio` process (not gunicorn-per-request — this needs to be always-on during market hours, which is a different operational shape than the rest of TOMIC).

```
SPX market data (DXLink)      account & position stream (AlertStreamer)
            │                              │
            ▼                              ▼
                  signal engine (0DTE entry/exit rules)
                              │
                              ▼
        pre-trade risk gate ──────► kill switch (halts both)
                              │
                              ▼
                order execution engine
                              │
                              ▼
                    tastytrade API (sandbox → live)

  position risk watchdog: separate task, watches OPEN positions
  continuously, independent of the signal engine, can trigger
  exits or trip the kill switch on its own.

  everything writes to: hash-chained audit log
```

### Component notes

- **Pre-trade risk gate** and **position risk watchdog** are two distinct tasks, not one. The gate is a one-time check before an order is sent (position limits, daily loss not yet breached, data freshness). The watchdog runs continuously against already-open positions (live delta/gamma/NLV) and can act independent of whatever the signal engine is doing — gamma risk in the final hour doesn't care whether entry logic is running.
- **State model:** the broker (via REST + AlertStreamer) is the only source of truth, always — not just on restart. Local SQLite is a cache and audit log, never the thing trusted when they disagree.

### Event loop discipline

One persistent process, one event loop, for the life of the trading session — market data ingestion, the signal engine, the watchdog, and kill-switch responsiveness all share it. A single blocking call anywhere (a synchronous disk write, a synchronous HTTP call, an expensive CPU-bound calculation) stalls all of them simultaneously, not just the task that made the call — including the watchdog's ability to react to a gamma spike, which is the one thing this system absolutely cannot afford to be late on.

**Rule: no synchronous I/O directly on the loop.** Disk writes and any non-async network calls go through `asyncio.to_thread` (or a genuinely async client), never a bare blocking call. See §6 for how this applies to the audit log specifically, since it's the highest-volume I/O in the system and the place this is easiest to get wrong.

## 5. Reuse vs. new-build

| Piece | Status | Notes |
|---|---|---|
| OAuth session pattern (`tasty_session.py`) | Reuse pattern, not credentials | Separate app registration — a bug here shouldn't touch TOMIC's session, and either should be revocable independently |
| `MarketDataService` / DXLink pattern | Reuse pattern, retune params | Existing `asyncio.Semaphore(3)` gating is tuned for scanning many symbols slowly; this needs one symbol at much higher frequency |
| `AccountStreamCache` / `AlertStreamer` | Direct reuse | Extend to also listen for `PlacedOrder` events (order fills) — confirmed as tastytrade's documented pattern, not just TOMIC's |
| SQLite + Litestream + numbered migrations | Direct reuse, tuned differently | Same durability story, new schema — WAL mode + `synchronous=NORMAL`, and every write goes through `asyncio.to_thread` so a slow disk (Litestream checkpoint, OS I/O spike) can't stall the event loop the way it would in TOMIC's request-scoped Dash callbacks, where a stalled write just slows one HTTP response instead of an always-on watchdog |
| Hash-chained audit trail (`ai_assessment_audit` pattern) | Direct reuse, higher stakes | Every signal, gate decision, and order — the black box recorder. See §6 for the tiered write path needed to keep this off the critical path without quietly weakening the guarantee it exists to provide |
| `RISK_CONFIG`-style hard caps | Reuse pattern | Extend static pre-trade gates with the real-time watchdog above |
| `is_stale()` staleness pattern | Reuse pattern, tighter tolerance | Monitor page uses ~30s; this needs a much shorter threshold |
| Dash / AG Grid UI | Do not reuse | Wrong tool — this UI is live status + kill switch, not a screener |
| Order placement, fills, multi-leg submission | New, from scratch | TOMIC has zero order code today |
| Order execution engine (price-walking, COA probe) | New, from scratch | See §7 |
| Crash recovery / reconciliation | New, from scratch | TOMIC has never held a position |

## 6. Non-negotiable safety rails

> **Historical framing, current content.** This section's rails are still exactly
> what CLAUDE.md's own "Non-negotiables" enforces today, but the "Definition of
> done... before Phase 1 sandbox" checklist below was satisfied long ago — Argus
> has been live since 2026-09-15 (CLAUDE.md's "Phase 2 — live, minimum size").
> Read CLAUDE.md and `docs/decisions/` for the built, current-state version of
> each rail; this section is the original design intent they were built from.

These are not hardening for later — for a fully automated system on a live account, they're the actual product.

- **Kill switch** — reachable in one action, from a phone. Halts new entries immediately regardless of signal engine state.
- **Daily max-loss circuit breaker** — auto-halts new entries the instant a daily loss threshold is hit. Trips automatically; doesn't wait to be noticed.
- **Fail closed on stale data** — market or account stream stale past threshold → risk gate blocks new entries by default.
- **Continuous reconciliation** — broker state is authoritative on every check, not just at startup. On any restart, refuse to resume automated logic until broker positions and internal state match exactly.
- **Sandbox first, with a real track record** — shadow mode → sandbox order submission → live at minimum size → scale only after live results back the edge up. See §11.

### Definition of done — safety rails (must all be true before Phase 1 sandbox)

- [ ] Kill switch reachable via single authenticated endpoint, halts new entries within a defined SLA
- [ ] Daily loss circuit breaker halts new entries automatically on threshold breach, no human step required
- [ ] Process reconciles broker state on every startup; refuses to resume on mismatch and alerts instead
- [ ] Market/account data staleness beyond threshold blocks new entries
- [ ] Every signal, gate decision, and order writes to the hash-chained audit log before the next action proceeds

### Audit log write path (performance vs. the "before the next action" guarantee)

The last DoD bullet above is what makes this a trustworthy black-box recorder rather than best-effort logging — it exists so that if the process crashes right after placing an order but before logging it, the trail doesn't have a hole at the one moment you can least afford one. That guarantee has to survive contact with §4's single-event-loop reality without either (a) blocking the loop on disk I/O, or (b) getting quietly weakened to fix (a).

Two tiers, not one:

- **Tier 1 — sequenced.** Pre-trade gate decisions immediately preceding an order, order submission/fills, kill-switch trips, daily-loss-breach events. These are exactly what the DoD bullet protects. Keep them synchronous from the *caller's* perspective — the coroutine awaits the write before proceeding — but route the actual disk I/O through `asyncio.to_thread` so a slow write doesn't stall other tasks on the loop while it's in flight. WAL mode + `synchronous=NORMAL` keeps typical write latency sub-millisecond; `to_thread` is the protection against the outlier case (a Litestream checkpoint or OS I/O spike stalling the loop for hundreds of ms during the exact volatility spike the watchdog needs to stay responsive through).
- **Tier 2 — high-volume telemetry.** Signal evaluations that resulted in no action, routine re-checks. These don't carry the crash-survival requirement — losing a handful of no-op log lines in an ungraceful crash isn't a safety gap. A decoupled queue + background writer (producer pushes, consumer computes the hash chain and writes) is the right pattern here. Bound the queue and alert on depth; do not let it grow without limit. Unbounded growth is most likely during exactly the high-volatility burst when Tier 2 volume spikes, and an OOM-kill there relocates the crash-recovery problem instead of solving it — the opposite of fail-closed.

Do not extend the Tier 2 pattern to Tier 1 events. A fire-and-forget queue removes the ordering guarantee the DoD bullet exists to provide — an in-memory event that hasn't reached disk yet is indistinguishable from an event that never happened, right at the moment (a crash) this system most needs to tell the difference.

## 7. Order execution engine

### Hard rules

- **Single-legging guardrail:** never route spread legs as separate orders. Always a single multi-leg complex order, so the exchange handles the fill atomically. Getting filled on the short leg only, naked, is not an acceptable failure mode.
- **Zero-bid guard:** before computing implied mid, floor any leg with `bid == 0.0` to the SPX minimum tick ($0.05). An unfloored zero-bid leg produces a mathematically unfillable mid.
- **No broker-side stop-market orders.** SPX bid-ask spreads can blow out temporarily during a spike; a stop-market order fills at the worst available price during exactly the moment you can least afford it.

### Price-walking loop (entry and exit both use this)

1. Calculate implied mid (zero-bid legs floored first)
2. Route as a single complex order at mid — this probes the Cboe Complex Order Auction (COA), which gives market makers a window to price-improve before the order either fills or books
3. Wait one cycle (tunable — see open question below)
4. Filled? → log fill via `AlertStreamer`/`PlacedOrder` event (Tier 1 audit write, §6), exit loop
5. Not filled? → cancel & replace, stepping price toward the market (e.g. $0.05 increments)
6. Walk-away threshold hit (e.g. 10% of max profit)? → abort entry, alert. Otherwise repeat from step 3.

**Exit variant:** the position risk watchdog computes a virtual stop — mid-price or underlying SPX crossing a threshold, sustained for several consecutive seconds to filter out a momentary spread blowout — and on trip, hands off to this same loop on a faster cadence (e.g. 1s vs. 3-5s for entries) to force the exit through the auction rather than into a blown-out market.

**Do not hardcode the COA auction timing.** Cboe has changed the SPX-specific response window repeatedly (100ms → 250ms → 500ms, plus a separate extended-processing-time allowance pushed as high as ~1000ms, renewed multiple times, most recently through a sunset date that has already passed as of this writing). Pull the current value from Cboe's published product configuration, or make it a runtime-adjustable parameter — never a literal in the code.

## 8. Position sizing (illustrative — not a recommendation)

> **Historical — superseded by the live-minimum-size numbers.** "2% until Phase 2"
> below is now the SANDBOX value only; live has run at `position_size_pct = 0.03`
> since the cutover. The real, current caps (`max_loss_per_position`,
> `max_contracts_per_position`, `max_concurrent_positions`, `daily_max_loss`, and
> the welded invariant between them) are in `argus.live.toml` and CLAUDE.md's
> "Standing landmines" — none of the illustrative numbers below were carried into
> the built system as literal targets.

SPX multiplier is $100/point. Numbers below are illustrative only; real sizing should come from your own backtest/shadow-mode data.

| Profile | Width | Credit | Max profit/contract | Max loss/contract |
|---|---|---|---|---|
| High win-rate (~10-15Δ) | $5 | ~$0.75 | $75 | $425 |
| High win-rate (~10-15Δ) | $10 | ~$1.50 | $150 | $850 |
| Moderate (~20-25Δ) | $5 | ~$1.50 | $150 | $350 |
| Moderate (~20-25Δ) | $10 | ~$3.00 | $300 | $700 |

For a brand-new, unproven strategy, size well under TOMIC's existing 5%-of-account cap — 2% until Phase 2 produces a real track record (see §11).

Remember these are theoretical max-loss figures assuming a hold-to-worthless outcome. The watchdog's virtual stop should mean realized average losses come in well under this column — which is the economic argument for building it, not just the safety argument.

## 9. Risk register

> **Historical — two mitigations named here are no longer what's built.** The
> "sustained-threshold virtual stop" row predates the strike-touch stop
> (`credit_trigger_enabled = false` since v0.5.2 — see CLAUDE.md's "Current
> phase"), and the daily-loss breaker row was aspirational when written (it could
> not trip in cert; it can and does in live now). The "shadow-mode track record"
> and "sandbox before live" rows describe steps already completed — see §10's own
> note. Read this table as the original risk-identification pass, not a current
> status board.

| Risk | Category | Mitigation |
|---|---|---|
| Gamma/tail event within a session | Strategy | Position risk watchdog, sustained-threshold virtual stop |
| Correlated daily loss (no intraday diversification) | Strategy | Daily loss circuit breaker as a hard stop |
| Edge decay / crowded 0DTE market | Strategy | Shadow-mode track record before sizing up; re-evaluate periodically |
| Signal engine logic bug | Engineering | Shadow mode first; sandbox order submission before live |
| Duplicate/runaway order submission | Engineering | Single-legging guardrail; idempotent order construction; streamed fill confirmation instead of polling |
| Stale data → bad decision | Engineering | `is_stale()`-pattern fail-closed gate |
| Crash mid-day, no reconciliation | Engineering | Continuous reconciliation against broker as source of truth |
| Event-loop stall from blocking I/O delays watchdog/kill-switch response | Engineering | `asyncio.to_thread` for all disk/network I/O off the loop (§4); tiered audit writes (§6); event-loop-lag heartbeat as an early-warning signal |
| tastytrade API outage/degradation | Operational | Fail-closed on stream disconnect; kill switch as manual backstop |
| Solo-developer blind spot | Operational | Kill switch reachable from phone; conservative initial position sizing |

## 10. Rollout plan

> **Historical — steps 1–3 are DONE; Argus is currently in step 3.** Shadow mode
> and sandbox order submission both ran to completion; the live-minimum-size flip
> was performed 2026-09-15 (`docs/live-cutover-runbook.md`). Step 4 (scale) has
> not started — see CLAUDE.md's "Current phase" for what "live, minimum size"
> means today and what still gates scaling up.

1. **Shadow mode** — log every signal and gate decision, submit nothing. Validate signal quality against live data for weeks before touching order code.
2. **Sandbox order submission** — tastytrade sandbox environment. Validate execution mechanics, reconciliation, and crash recovery under fire, with no real money at risk.
3. **Live, minimum size** — 1 lot, full safety rails active, close human monitoring.
4. **Scale** — only after a real live track record, not shadow-mode results alone.

## 11. Open questions

> **Several of these are now answered and built — this list was never pruned.**
> The daily loss cap, the position size cap, the COA timing source, and separate
> tastytrade app credentials are all decided and live (`argus.live.toml`,
> `[coa] source`, CLAUDE.md's Credentials section); the Tier 2 audit queue bound
> is set (`tier2_queue_maxsize`/`tier2_queue_alert_depth`). Only the cancel/replace
> cadence and dashboard scope questions still map to open, evolving work — see
> `docs/decisions/README.md`'s own open-questions table for what is genuinely
> still unmeasured.

- Exact cancel/replace cadence — needs confirmation of tastytrade's actual rate limits before finalizing the price-walking loop timing (engineering)
- Exact daily loss cap $ value (Richard)
- Exact position size cap % to start at, and the graduation criteria to move toward TOMIC's 5% norm (Richard)
- Where COA timing config is sourced from — polled from Cboe product config on startup, or manually updated on notice (engineering)
- Monitoring dashboard scope — what fields, beyond status/P&L/recent decisions/kill switch (Richard)
- Separate tastytrade app credentials — recommended, needs to be registered (Richard)
- Tier 2 audit queue: bound size and backpressure/alert threshold (engineering)
- Event-loop-lag alert threshold, and whether a sustained stall should itself trip the same fail-closed gate as stale data (Richard)

---

*Source conversation: TOMIC strategy roadmap review, 0DTE architecture design, risk/reward breakdown, and execution-engine assessment of external reference material (Cboe COA mechanics, tastytrade streaming order updates — both verified against current documentation as of 2026-07-14).*

*2026-07-17 revision: reviewed an external proposal to move the hash-chained audit log onto a fire-and-forget `asyncio.Queue` to keep it off the execution critical path. Adopted the producer/consumer pattern for high-volume, non-safety-critical telemetry (Tier 2), but rejected it for gate decisions/order events/kill-switch trips (Tier 1) — an unpersisted in-memory event is indistinguishable from one that never happened if the process crashes before the consumer catches up, which defeats the exact guarantee the audit log exists to provide. Added §4 event loop discipline and §6 tiered write path to resolve the underlying latency concern (event-loop blocking on disk I/O) without weakening that guarantee.*