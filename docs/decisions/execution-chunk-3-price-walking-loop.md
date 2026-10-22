# Order execution engine — chunk 3: the price-walking loop (spec §7)

*The price-walking cancel/replace state machine and the request governor.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


`execution/loop.py` is spec §7's loop, and `execution/ratelimit.py` is the
process-wide request governor that guards it. Chunk 3 owns TIMING and SEQUENCING
and nothing else: it builds no orders, prices nothing itself, writes no ledger
row, and decides nothing about whether a trade is worth making. It is handed a
built order and gets it filled or gives up. Standalone like chunks 1 and 2 —
nothing calls it, and it has not been run against the real API. Chunk 4 added ONE
thing to `loop.py`: `WalkPolicy.for_exit` (a policy factory next to `for_entry`,
so the two-key COA settle assembly exists once). The state machine is byte-for-byte
unchanged and knows nothing about exits; a test asserts `loop.py` never imports
`watchdog.py`.

Decisions that are settled — do not relitigate:

- **ONE state machine, `WalkPolicy` is the only difference between entry and
  exit.** Chunk 4's exit is a different policy instance (faster cadence, its own
  concession), never a second implementation — two copies of a cancel/replace
  machine is how the exit path, the one that runs when a position is already
  hurting, drifts from the entry path that gets all the testing. The machine has
  **no direction branch anywhere**, and that falls out of tastytrade's sign
  convention rather than being invented here: limit price is positive for a credit
  and negative for a debit, so "concede toward the market" is *subtract a tick* for
  both (`+0.85 → +0.80` collects less; `-0.85 → -0.90` pays more). `PriceWalk` is
  pure and routes every price through `zerobid.round_to_tick`.
- **The cycle clock starts on a CONFIRMED in-market status, not on the ack.**
  `models.CONFIRMED_IN_MARKET` is `{LIVE}` and deliberately nothing else. Chunk 1
  MEASURED the difference: a pre-market submission is accepted, carries
  `tif.next_valid_session`, and sits at `Received` outside the market. The
  asymmetry settles the rest — too strict costs latency (the clock starts late, the
  walk is slower); too loose is the measured failure (the cadence runs while the
  order is not in the auction, and the first reprice cancels an order that never
  had a chance). `ROUTED`/`IN_FLIGHT`/`CONTINGENT` are NOT in the set because
  nobody has measured them. **Widening it is a measurement, not a judgement call:**
  watch a real order's `AlertStreamer` transitions during regular hours. A
  submitted order that never reaches the market inside `working_timeout_s` is
  CANCELLED, never left — `next_valid_session` means it goes live at the next open,
  unattended, at a price nobody is walking.
- **A cycle is `max(cadence, response_window + extended_processing)`.** Both COA
  figures, never the response interval alone (see the non-negotiable above). A
  cadence configured tighter than the auction is raised to it rather than allowed
  to race it.
- **The stale-reference risk is answered by a CIRCUIT BREAKER, not by re-quoting**
  (follow-on, 2026-07-30). The ladder below is deterministic from the gate-approved
  mid, which is deliberate — but at 10-15 delta a 2-3 point SPX move inside a 20-30s
  walk moves a 5-wide spread by a large fraction of a $0.65 credit, and every rung
  after that is priced off a market that has moved on. `WalkOutcome.DRIFTED` is the
  answer: an OPTIONAL `drift_check: Callable[[], str | None]` injected per walk,
  called at the cycle boundary, that abandons and cancels when the reference no
  longer holds. The loop still imports nothing about market data and `price_at`'s
  call graph is still `{round_to_tick}` — both AST tests pass unmodified, and
  `drift_check=None` is byte-for-byte the shipped behaviour. It returns a REASON
  rather than a bool because the reason is the diagnostic that lands in
  `walk.finished`. **Ordering is the load-bearing part**: the check runs AFTER
  `_resolve` (a fill seen in the same cycle wins — cancelling something already
  filled is far worse than walking one cycle too long) and AFTER the blind-cycle
  branch (an order that could not be read gets no verdict of any kind). A check that
  RAISES abandons the walk rather than propagating: it is in-memory arithmetic, so an
  exception is a defect in the callable, and propagating would hand it to chunk 5's
  hard-halt region — while this path cancels the order, so the live-order-nobody-is-
  walking condition that justifies a halt never arises. **`DRIFTED` is deliberately
  NOT `WALKED_AWAY`**: both end with a cancel and no fill and they mean opposite
  things — walked-away is evidence about `entry_walk_away_pct` (budget spent, auction
  would not pay), drifted is evidence about nothing but the tape (pulled with budget
  left). Collapsing them corrupts the one statistic the walk-away threshold gets tuned
  from. Scope is ENTRIES only; whether exits should also arm it (to cross sooner
  rather than wait out the ladder) is deliberately undecided — chunk 4 untouched, and
  its `_after_walk` already treats any non-FILLED/non-WALKED_AWAY outcome as
  `NOT_RESOLVED`, so an outcome it can never receive is still handled correctly.
- **The reprice ladder is DETERMINISTIC from the gate-approved mid — the loop never
  re-quotes.** `loop.py` imports `TICK`/`round_to_tick` and nothing else from
  `zerobid`, takes no `MarketDataPort`, and `PriceWalk.price_at` calls exactly one
  function. Two AST tests pin both. This diverges from the chunk-3 brief's
  "recompute the limit price through `zerobid.py`/`build.py`", deliberately: spec §7
  step 5 is "step the price toward the market", a step from the current limit, not a
  refetch; the walk-away threshold is only auditable ("I conceded 10%") against a
  FIXED reference; and `build.py`'s `_assert_credit_matches_legs` exists to stop a
  limit coming from anywhere but the gated floored mid, which an in-loop refetch
  would end-run. The tradeoff, stated so it can be overruled: in a fast market the
  ladder concedes from a reference that no longer matches the mid, so it can give up
  more than necessary (or hit its floor while the market has moved). **The reason
  this is also a SAFETY property:** an order's legs carry OCC symbols while
  `MarketDataPort` is keyed by `streamer_symbol` — `market.latest(order_leg.symbol)`
  returns `None` forever without raising, which is the bug chunk 4 hit and fixed with
  `LegQuotes`. Having no quote path is why the loop was never exposed. If fresh-quote
  repricing is ever wanted, it goes through `LegQuotes` — never a second resolver,
  and never `order_leg.symbol`.
- **"I could not read the order" never reprices and never resolves silently.** The
  loop's one lookup that can return `None` without raising is the BROKER read
  (`OrderView.by_id` when the order is absent from `get_live_orders()`). It is
  treated as a read gap, not a verdict: the cycle does NOT step the price, and
  `blind_cycles` bounds it at `max_recovery_attempts` consecutive misses before
  `UNRESOLVED` + alert. Same for never-visible-at-all (`walk_order_not_visible`) and
  for reads that raise (retry with backoff, then `UNRESOLVED`). Every one is Tier 1
  and pages someone; none is a stale-price reprice.
- **The loop refuses anything but `AuditedOrderExecution(..., store=)`,
  structurally.** `PriceWalkingLoop.__init__` raises `TypeError` on a raw port and
  `ValueError` when `records_orders` is False (the new public property on the
  decorator). Chunk 1's adapter writes no pre-send record and no ledger row, so a
  walk driving it directly places orders that are invisible to the audit trail and
  unaccounted for by the next restart's order reconciliation — which hard-halts
  sandbox/live. That failure surfaces the morning after, in the one component whose
  job is to refuse to guess, so it is caught at construction instead.
- **The guard runs exactly once, before the first submit.** `run()`'s
  `guard_first_submission` has NO default, like `submit`'s `dry_run`. It is never
  consulted again inside the call: a replace re-sends the same
  `external_identifier` against its own still-working order, so a guard check there
  would find that order, correctly call it a conflict, and deadlock the loop
  against itself. A guard failure PROPAGATES rather than becoming an outcome —
  turning "I could not check" into a return value invites a caller to treat it as
  one more not-filled case.
- **`replace()` for every reprice; standalone `cancel()` only for abandonment.**
  Cancel-then-submit by hand opens a window with nothing working and nothing
  recorded as intended. A returned `cancel()` means `CANCEL_REQUESTED`, not gone —
  so **`WalkResult` is a REPORT, not a guarantee**: a fill can cross a cancel, and
  `positions` (chunk 2) plus reconciliation stay authoritative. Nothing downstream
  may read a `WalkResult` as proof of what is or is not held.
- **Recovery asks the broker; it never re-sends.** A mid-loop disconnect is chunk
  3's own problem and must NOT escalate to a process halt (reconciliation is a
  restart-time rail). Reads are retried freely; a failed MUTATION is resolved by
  reconnecting, re-querying `by_identifier`, and resuming from what exists — a
  replace that actually landed must not be replayed. The step counter is
  re-derived from the broker's own limit price (`PriceWalk.step_of`), because a
  failed replace leaves the local counter claiming a rung the order may never have
  reached. **Two working orders under one identifier → cancel BOTH and return
  UNRESOLVED**: the broker does not enforce uniqueness (measured, chunk 1), both
  could fill, and missing an entry costs a trade while an unmanaged duplicate costs
  the risk limit.
- **A partial fill takes what filled and cancels the remainder.** Because a
  vertical is ONE multi-leg order, a partial is a SMALLER SPREAD — defined-risk,
  never a naked leg. The remainder is not walked because tastytrade's replace
  semantics on a partially-filled order have NOT been measured, and the way to be
  wrong is to re-request the full size and end up with double the position.
  Measure the replace semantics before changing this.
- **Outcomes are explicit, not success/failure**: `FILLED` (with a contract count;
  `None` means the broker never said), `WALKED_AWAY`, `REJECTED` (ends immediately
  — not a "not filled yet" case), `BLOCKED`, `NEVER_WORKED`, `CANCELLED_ELSEWHERE`,
  `UNRESOLVED`, `ABORTED`. `recovered_disconnects` carries the
  disconnected-then-recovered case. Chunk 5 needs all of these apart.
- **The rate governor is insurance, not cadence.** 40 req/s per IP on `/orders` is
  CONFIRMED (tastytrade support, 2026-07-29); designed volume is under 1/s, so
  nothing about the loop's cadence is shaped by it and nothing should be.
  `ThrottledOrderExecution` is a decorator so it covers every order request in the
  process, including ones made by code that never heard of it. Layering is
  **audit OUTSIDE, throttle INSIDE** — the Tier 1 write must be durable regardless
  of how long the wire waits for its slot. A minimum interval, deliberately not a
  token bucket: a bucket lets a runaway defect empty its burst before the brake
  engages, which is the one case this is for.
- **Walk-away threshold, cadence and the drift threshold are config, and are now
  CHOSEN — but only for SANDBOX ENTRY.** `[execution] entry_cadence_s` /
  `entry_step_ticks` / `entry_walk_away_pct` / `entry_drift_abort_points` are
  Richard's call, made 2026-08-10. The loader requires a non-empty `walk_source`
  provenance note — the same rule `coa.source` follows — and the shipped value is
  no longer `UNSET PLACEHOLDER`: it names who chose them, why, and what each was
  checked against (`docs/execution-watchdog-parameters-2026-08-10.md`). That
  mechanism is unchanged and still the point — provenance is a fact stated in the
  deployed config and visible in every review, rather than a comment somebody edits
  around. **They are explicitly NOT final live-scale values**: `walk_source` records
  a second data-backed pass before size is scaled, and flags
  `entry_walk_away_pct = 0.10` as the one to watch (84% of shadow candidates take
  exactly one $0.05 reprice before the floor, and a walk-away RETIRES that strike
  pair for the process lifetime). `entry_drift_abort_points` is REQUIRED (a missing key
  refuses to start — a defaulted one would be a guessed number that abandons live
  entries) and is covered by the same `walk_source` note rather than a fourth
  provenance key. Points, not a formula: a formula needs delta/width assumptions that
  belong to the strategy, not the loader.

Audit categories added: `walk.started`, `walk.finished`, `walk.disconnected` (Tier
1); `walk.holding`, `walk.step` (Tier 2 — a reprice is high-volume and survivable,
how a trade idea ENDED is not). The per-call `order.*` records still come from
`AuditedOrderExecution`; the loop does not duplicate them.

Tests run against fakes only. `tests/test_execution_loop.py` drives the real
`AuditedOrderExecution` on a **fake clock that advances only when the loop sleeps**
— a suite about waiting cannot afford to wait — and an AST test pins that `loop.py`
never calls `asyncio.sleep` directly or imports `TastytradeOrderExecution`.
