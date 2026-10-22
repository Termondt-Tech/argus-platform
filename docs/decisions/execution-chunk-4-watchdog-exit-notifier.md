# Order execution engine — chunk 4: watchdog, exit handoff, notifier (spec §4/§7)

*The position risk watchdog, the exit handoff, and the shared Slack notifier.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


`execution/watchdog.py` is spec §4's position risk watchdog — a task that is
deliberately independent of `SignalLoop` ("gamma risk in the final hour doesn't
care whether entry logic is running") — and `notify.py` is the alerting path
`alerts.py` has declared since week one. Chunk 4 owns WHEN to exit; it delegates
HOW to chunks 1–3 and adds exactly one thing of its own to the order path: the
market-crossing price.

Decisions that are settled — do not relitigate:

- **The entry credit comes from the ORDER LEDGER, not from `positions`.** The brief
  for this chunk assumed a credit field on the position record; there isn't one.
  `positions` (migration 0002) is keyed by OCC LEG symbol, has no spread-level row,
  and its only price column (`average_open_price`) is documented in `store.py` as
  best-effort and safety-irrelevant — 0.0 whenever the broker reported no per-fill
  prices, which is every case chunk 1 actually observed. So `store.open_spreads()`
  reassembles spreads from `orders`/`order_legs` (the only place the two legs are
  grouped at all) and takes `entry_credit` from `orders.limit_price` on the filled
  opening order. That is the LIMIT, not the realized fill: for a limit order the
  realized credit is at least the limit, so the basis is at worst slightly low and
  the 2x trigger fires slightly EARLY — the safe direction. **No migration was
  added** (considered and rejected: 0005 recording a spread-level realized credit
  buys accuracy nobody needs yet and couples this chunk to a deploy-ordered schema
  change). Revisit only if realized-vs-limit divergence is ever measured to matter.
  `contracts` comes from `positions`, never from the order — the order says what was
  requested, the position says what is held.
- **The OCC symbol an order references is NOT the symbol market data is keyed by,
  and that is the single most dangerous thing in this chunk.** `OptionContract`
  pairs `symbol` (OCC, what an order carries) with `streamer_symbol` (dxfeed, what
  `MarketDataPort` is keyed by) and they are different strings. `market.latest(
  order_leg.symbol)` returns `None` forever — a stop that never fires, silently,
  discovered by losing money. `LegQuotes` is the one resolution step, it reuses the
  chain the signal engine already subscribed (no second quote path), and it returns
  a `LegResolution` that STATES why it failed rather than a bare None.
- **"I cannot evaluate this" is never "no trigger".** A missing or stale quote does
  not clear a sustained condition and does not reset its clock — there is no
  evidence the condition went away, and for a watchdog the fail-closed direction is
  toward acting. Staleness is `argus.staleness.is_stale`, the same one definition
  the gate uses.
- **The two triggers are evaluated INDEPENDENTLY.** The underlying trigger needs
  only spot and the short strike, both of which survive an options-feed gap — which
  is exactly the blown-out-spread scenario it is the backstop for. If it fires while
  the legs cannot be priced, no order can be built: that is recorded, alerted, and
  RETRIED next cycle rather than consuming the position's one exit attempt.
- **The debounce is per (position, reason) and never accumulates across episodes.**
  Two 1.5s episodes are not one 3s episode. Each reason has its own window, so
  "whichever fires first" does not hand the second trigger a head start.
- **One exit attempt per position, ever, per process.** An exit that reached the
  broker is never retried: a second closing order could stack on the first and close
  the spread TWICE, which does not flatten it — it re-opens it inverted, unmanaged,
  on expiry day. Everything except a clean fill alerts, so a position left open here
  is one a human has already been told about.
- **`WALKED_AWAY` is escalated here, and the cross is REFUSED rather than stacked.**
  `cancel()` leaves an order at `Cancel Requested`, not gone (chunk 3, property 3),
  so before the crossing order goes out the abandoned one must be confirmed
  terminal — bounded by `watchdog.exit_cancel_settle_s`. **Confirmed means POSITIVELY
  confirmed**: the broker names the order and every disposition it gives is terminal.
  Anything else at the deadline → no cross, Tier 1, alert. Absent from the answer is
  one of those (2026-08-21 — `any()` over an empty list is `False`, so an order nobody
  read used to pass for one confirmed dead;
  [detail](2026-08-21-empty-broker-answer-is-not-a-verdict.md)), alongside still
  working and unreadable; the refusal names which. Filled meanwhile → `FILLED_LATE`,
  no cross.
  Only `WALKED_AWAY` is crossed: `REJECTED`/`NEVER_WORKED`/`UNRESOLVED`/`BLOCKED`/
  `CANCELLED_ELSEWHERE`/`ABORTED` are not "the auction did not pay enough" and are
  not answered by paying more.
- **The crossing price is the NATURAL cost, and the zero-bid floor is deliberately
  not applied to it.** Pay the short leg's ask, receive the long leg's bid.
  Flooring the long bid up to a tick would LOWER the debit and make the one order
  whose entire purpose is to fill less aggressive. Capped at the spread WIDTH — a
  vertical can never be worth more than its strike distance, so a quote implying
  otherwise is a bad print and paying it would turn a bounded loss into an unbounded
  one. It reaches the wire via `order.with_limit_price(...)`, the same model method a
  reprice uses, NOT a rebuild (`build.py` asserts credit-matches-legs, and a cross
  is deliberately not a mid).
- **The exit RUNS the duplicate guard** (`guard_first_submission=True`). The exit
  carries its own brand-new `external_identifier` (`argus-close-<candidate>`), so
  the guard's rule applies exactly; the scenario is a crash mid-exit, a restart, the
  position still open. A `BLOCKED` exit means a close is already in flight — the
  right answer, and one that must never be crossed on top of. Consequence worth
  knowing: the guard RAISES when it cannot reach the broker (chunk 2) and this chunk
  halts on any exception, so an unreachable broker at exit time halts and pages
  rather than retrying quietly. Intended — a transient blip is already absorbed
  inside the walk, whose reads retry `max_recovery_attempts` times.
- **Exception policy is HALT THE PROCESS, and it was signed off explicitly** rather
  than chosen by default. Any unhandled exception in the watchdog task (or in an
  exit task) engages the kill switch and requests stop via the same
  `Application._halt_process` pairing the audit-failure and reconcile-mismatch
  paths use, `source="watchdog_exception"`. This is deliberately stricter than
  `SignalLoop`, which logs a bad iteration and carries on — right for a loop that
  submits nothing and holds nothing, wrong here, where what a crashed iteration
  leaves behind is a real position with nobody watching it.
- **Exits run as their own tasks.** An exit walk holds each price for at least a
  full COA settle window and may step several times; doing it inline would leave
  every OTHER open position unchecked for the duration, which on a two-position
  book is half the risk unwatched at the worst moment.
- **The ledger is re-read on a SLOW interval, not per cadence tick.**
  `store.open_spreads()` runs through `AuditLog.read`'s writer-connection lock, whose
  contract is "reads are RARE — never an automated hot path". So `[watchdog]
  cadence_s` drives trigger evaluation (pure in-memory quote arithmetic, no DB) and
  `refresh_interval_s` drives the ledger read; the loader refuses
  `refresh_interval_s < cadence_s`. `PositionWatchdog.request_refresh()` is chunk 5's
  wiring point — call it from the fill consumer so a new position is watched
  immediately rather than up to an interval later.
- **Anything held but unexplained is Tier 1 and pages someone.** A `positions` row no
  filled opening order accounts for, a ledger order that is not a well-formed 2-leg
  vertical, a leg imbalance (part of the position naked) — `OpenSpreadScan` carries
  all of it back rather than silently returning only the spreads it understood. This
  is Argus's own two records disagreeing, distinct from reconciliation (which
  compares against the broker) and just as much a gap.
- **Repetitive conditions escalate on the RISING EDGE only** (`_Escalation`). A feed
  gap makes a position unevaluable on every tick and a triggered-but-unpriceable
  position fails to build an order on every tick; a Tier 1 write per tick is not more
  safety, it is a trail nobody can read plus sustained contention on the lock spec §6
  protects. One Tier 1 record + alert per episode, Tier 2 for the repeats, Tier 1
  again on recovery — the same shape `account/stream.py` uses.
- **Trigger values are config and are now CHOSEN — but only for SANDBOX ENTRY.**
  `[watchdog] credit_multiple` / `underlying_buffer_points` / `sustained_s` are
  Richard's call, made 2026-08-10; the loader requires a non-empty `trigger_source`
  provenance note (same rule as `coa.source` and `execution.walk_source`) and the
  shipped value is no longer `UNSET PLACEHOLDER` — it names who chose them and why
  (`docs/execution-watchdog-parameters-2026-08-10.md`). `credit_multiple = 2.0` is
  now backed by direct 0DTE practitioner precedent (real range 2.0-2.5x) rather than
  only by longer-dated tastytrade-lineage convention, and 2.0 is the tighter end.
  `underlying_buffer_points = 5.0` is recorded as a LIQUIDITY BACKSTOP, to be
  validated by `watchdog.exit_unpriceable`/`watchdog.unwatchable` frequency during
  wide-quote episodes, NOT by trigger rate on an ordinary day. **Not final
  live-scale values** — a second data-backed pass is planned before scaling size.
  `credit_multiple` must be > 1 (at 1.0 the trigger fires at break-even — an
  immediate exit, not a stop) and `sustained_s` >= `cadence_s` (or one observation
  satisfies the debounce and it does nothing).

Audit categories added: `watchdog.triggered`, `watchdog.exit_started`,
`watchdog.exit_finished`, `watchdog.crossed`, `watchdog.exit_unpriceable`,
`watchdog.exit_unattempted`, `watchdog.unevaluable`, `watchdog.evaluable_again`,
`watchdog.unwatchable`, `watchdog.leg_imbalance`, `watchdog.fatal` (Tier 1);
`watchdog.check`, `watchdog.triggered_still`, `watchdog.unevaluable_still`,
`watchdog.exit_unpriceable_still`, `watchdog.exit_unattempted_still` (Tier 2 — a
"checked it, no action" is the common case by volume and would otherwise drown the
Tier 1 trail). This chunk is the SECOND consumer of the never-pinned Tier 2 queue
bound/threshold — still a pre-existing open item, but it now has two consumers.

## The shared alert notifier (`notify.py`)

`alerts.py` declared `AlertFn` in week one with "the phone-reachable alerting path
plugs in here later", and everything that needs a human has called it ever since —
audit queue depth and write failure, heartbeat stalls, reconcile mismatches, session
errors, chunk 2's foreign fills, chunk 3's walk failures. The only implementation was
`log_alert`. `notify.SlackNotifier` is the real one, and `Application.startup()` now
resolves it process-wide, so every one of those existing consumers reaches a phone
without any of them changing.

- **It lives at `argus/notify.py`, not `execution/notify.py`** (which is where the
  chunk brief put it). Half its callers sit above the execution package; importing
  `argus.execution.notify` from `app.py` would invert the layering for a
  process-wide facility. It sits next to `alerts.py`, whose type it implements.
- **`ARGUS_SLACK_WEBHOOK_URL` from env, never config or code, never logged in full.**
  An incoming-webhook URL *is* the authentication, and the secret is entirely in the
  path — so `redact_webhook()` yields scheme + host only, error paths carry that, and
  `scrub()` strips the URL out of any message this module did not write. `https://`
  is required. **OPTIONAL, unlike `ARGUS_KILL_TOKEN`:** a missing webhook is a missing
  notification channel, not a broken safety rail (every alert still reaches the log,
  every rail still works), so it must never refuse startup.
- **It never raises and never blocks.** `AlertFn` is SYNCHRONOUS and is called from
  `AuditLog.telemetry`, the heartbeat, and the price-walking loop, so a blocking
  `urlopen` inside it would stall the single event loop the watchdog and kill switch
  share (spec §4). `__call__` only enqueues onto a bounded queue; a background task
  POSTs through `asyncio.to_thread`. Every failure — full queue, no loop, an
  unserializable detail, Slack down — is counted and logged, never raised: an alert
  that can raise converts a handled failure into an unhandled one at the worst moment.
- **`raise ... from None` was not enough.** It clears `__cause__` but leaves
  `__context__` pointing at the `HTTPError`, which holds `.url` — so any traceback
  format would print the webhook. `_urllib_post` builds the message inside the handler
  and raises AFTER the `except` block, so there is no chained exception to leak from.
  A test pins this.
- **Stdlib `urllib`, not httpx**, for the same reason `control.py` is a plain
  `asyncio.start_server`: one fully-controlled request to one fixed URL does not
  justify a direct dependency in a `--require-hashes` image built for a minimal
  auditable supply chain.
- **Retrofits into already-merged code.** `FillConsumer` (foreign fills, documented
  "Tier 1 + alert") and `PriceWalkingLoop` (`max_recovery_attempts` exhausted →
  `walk_unresolved`, documented as alerting) both had `alert: AlertFn | None = None`
  and `if self._alert is not None`, so an omitted argument silently turned those
  documented alerts into nothing. Both now default to `log_alert`: forgetting to pass
  a sink degrades to a log line rather than to silence, and the real sink is what
  gets passed. Tests pin both defaults.

Not exercised against sandbox, same as chunks 2 and 3 — nothing calls the watchdog
yet. Two things chunk 5 should confirm when it wires this up: that a real fill's
`AlertStreamer` events land in `positions` fast enough that `request_refresh()` makes
the position watchable within a cadence tick, and what a real `Cancel Requested`
actually takes to go terminal (which is what `exit_cancel_settle_s` is guessing at).
