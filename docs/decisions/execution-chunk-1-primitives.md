# Order execution engine — chunk 1 primitives (spec §7)

*Order construction and submission primitives, plus what the 2026-07-29 sandbox run measured.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


`src/argus/execution/` holds the ATOMIC broker-interaction primitives and the
order domain model the rest of the arc is built on. Deliberately small in scope:
construct an order, and submit/cancel/replace/read one. **No looping, no cadence,
no retry, no recording, no wiring.** Chunk 3 owns deciding when to call these
again; chunk 2 owns the pre-send Tier 1 audit write and position recording.

Decisions that are settled — do not relitigate:

- **The limit price is the mid the GATE saw — never a refetch.** `build.py` reuses
  `SpreadSignal.credit` (the already zero-bid-floored net mid) for the first
  submission. Pulling a fresh quote at construction would submit a different trade
  than the one that was sized and risk-evaluated, and would add a round trip on the
  latency-critical path. `_assert_credit_matches_legs` recomputes the mid from the
  signal's own legs through `zerobid` and refuses to build on a mismatch, so a
  credit computed some other way can't become a limit price.
- **Freshness is a timestamp bound, not a refetch.** `SpreadSignal` now carries
  `captured_at` (straight from `ChainSnapshot.captured_at`, MONOTONIC seconds), and
  `build_order` refuses a candidate older than `[execution] signal_max_age_s` via
  `staleness.is_stale` — the same fail-closed rule the gate uses, so NaN and a
  backwards clock count as stale too. That catches a pipeline stall between
  gate-pass and construction with no I/O. The loader refuses a value larger than
  `staleness.market_data_max_age_s`, since the gate would block on the looser
  threshold first and the check could never fire.
- **`zerobid.py` is THE zero-bid floor** (spec §7: floor a `bid == 0` leg to the
  $0.05 min tick before taking a mid). `signal.models.OptionQuote.floored_mid`
  DELEGATES to it rather than reimplementing it — the signal layer asks "is this
  worth proposing?" once per evaluation, `build.py` recomputes it to refuse a
  credit that came from anywhere else, and chunk 4's WATCHDOG reapplies it to
  FRESH quotes to price a close. Same arithmetic, several needs, one
  implementation. (`test_zerobid.py` fails if a second copy of `SPX_MIN_TICK`
  appears anywhere.) **Chunk 3's price-walking loop is NOT a fresh-quote caller**
  — an earlier version of this bullet and of `zerobid.py`'s docstring said it was,
  and that was wrong: `loop.py` imports `TICK`/`round_to_tick` only and walks a
  deterministic ladder from the gate-approved mid. See chunk 3's section for why,
  and for what it would cost to change (the OCC-vs-`streamer_symbol` trap chunk 4
  hit). `round_to_tick` returns a `Decimal` —
  it is the boundary between the signal layer's float economics and the exact,
  tick-aligned price that goes on the wire, and it normalizes the input to 6dp
  first so accumulated float noise can't flip a tie.
- **`LimitOrder`, not `NewOrder`.** Same plain (non-complex) endpoint; `NewOrder`
  is deprecated in tastytrade SDK 13.x in favour of the per-type classes, so using
  it would emit a DeprecationWarning on every submission for no behavioural gain.
- **Status/action vocabulary is tastytrade's, verbatim.** `OrderStatus` and
  `LegAction` mirror the SDK's enums value-for-value, and `test_execution_models.py`
  asserts the value sets are EQUAL — an SDK that gains a status breaks CI instead
  of falling through a mapping on a live order. `PARTIALLY_REMOVED` is deliberately
  NOT terminal (fail toward "might still be working"), and an unknown status on an
  `Order` also counts as working.
- **Partial fills are quantities, not booleans.** Every leg carries
  `remaining_quantity`, with `None` ("broker hasn't said") kept distinct from `0`
  ("filled"). Reacting to a partial is chunk 2/3's problem; representing it
  honestly is the model's.
- **`replaces_order_id`/`replacing_order_id` are READ off the broker's response,
  never reconstructed** from what we think we sent.
- **`external_identifier` is a deterministic MATCHING key** — not, as it turns out,
  a broker-enforced one: `argus-<direction>-<YYYYMMDD>-<side>-<short>-<long>`, set
  once per trade idea at construction. The same candidate rebuilds to the same key
  after a crash, so `get_live_orders()` can answer "did I already send this?" from
  the BROKER — the only source of truth — instead of from local state that may be
  exactly what was lost. **tastytrade does NOT enforce uniqueness on it** (measured,
  see below), so that check is the ONLY duplicate guard there is. `candidate_id` is
  the same identity minus the direction, and is intentionally the tuple
  `shadow.outcome.trade_ideas` dedups on, so a live order joins straight to its
  shadow-mode history.
- **Four separate async calls behind `OrderExecutionPort`** (submit/cancel/replace/
  get_live_orders), never fused. Fusing them would leave chunk 2 no place to put its
  Tier 1 audit write between the decision and the wire. `submit`'s `dry_run` has NO
  default in either direction — routing vs. validating must be stated. `replace`
  has no dry-run form because tastytrade offers none: every replace is live.
- ~~`get_live_orders()` SKIPS (and logs) an order it can't map. Safe here
  specifically because this is not the reconciliation rail — spec §6 reconciles
  POSITIONS (`argus.reconcile`), untouched by anything dropped here — and a
  hand-placed sandbox order must not take out the call chunk 3 uses to find its own
  working orders.~~
  **SUPERSEDED 2026-08-20.** The second clause was true when this was written and
  false from the moment chunk 2 gave `argus.reconcile` an order-level check that
  calls `get_live_orders()`. The requirement it protects (one bad row must not fail
  the whole read) still holds; the drop does not. `get_live_orders()` now returns
  `LiveOrders` and CARRIES the unmappable ones, where reconciliation halts on them.
  → [unmappable orders](2026-08-20-unmappable-orders-reach-reconciliation.md)

**RESOLVED in chunk 3** (this section previously listed both as open): the
cancel/replace cadence and the rate limit are now in `[execution]`. tastytrade
support gave the authoritative figure on 2026-07-29 — **40 order requests/second
per IP on `/orders`** — which is ~50x the designed volume, so it shapes nothing
about the cadence and is enforced only as runaway insurance
(`execution/ratelimit.py`). The cadence and walk-away VALUES are config keys and
were **CHOSEN by Richard on 2026-08-10** — declared SANDBOX-ENTRY values, not final
live-scale ones; see chunk 3's section below.

**Validating it: `argus.tools.order_smoke`.** Unit tests run entirely against
`FakeOrderExecution` (no network in `pytest`), and a fake is only evidence about
the fake — notably `FakeOrderExecution` ENCODES the assumption that a replace mints
a new id and links the pair both ways. This tool is what checks that against the
real API, the same role `tools.account` played before anything was built on the
account primitives. It builds a real two-leg SPXW order from live sandbox quotes
and **dry-runs by default**; `--live-submit` submits/replaces/cancels once for
real, deliberately priced away from the market (mid + `--offset` credit, capped
below the spread width) so it books rather than fills, and cancels on the way out.
It refuses to run against `environment = "live"`.

```
./.venv/bin/python -m argus.tools.order_smoke --config argus.dev.toml
./.venv/bin/python -m argus.tools.order_smoke --config argus.dev.toml --live-submit
```

Run `--live-submit` **outside a session window** (before 09:40 ET or after the
close), same care as the reconciliation cutover, and confirm the account is flat
AFTERWARDS, not just before — the tool prints positions on both sides and warns on
any change. A test order that fills leaves a position, and the next session's
reconcile compares against a still-empty expected set, so it would refuse the day:
a lost day of shadow data for a smoke test.

## What the sandbox actually did (measured 2026-07-29, account 3AD79010)

Ran pre-market against cert; four orders placed and all four cancelled; account
FLAT before and after; reconciliation clean. Confirmed as designed: `Equity Option`
IS the instrument type for SPXW index options (the dry run would have failed
otherwise); the wire payload is one plain limit order with two legs and the sign
split into `price` + `price-effect` (`"price":"0.65","price-effect":"Credit"`);
`external_identifier` round-trips and survives a replace; `remaining_quantity` is
reported per leg; a dry run returns NO order id. Three findings that were NOT
assumptions going in:

- **`external_identifier` uniqueness is NOT enforced.** Submitting the same
  identifier a second time was ACCEPTED both while the first order was still
  working AND after it had been cancelled — two live orders, one identifier,
  different ids. So spec §9's "duplicate/runaway order submission" risk has **no
  broker-side backstop**: chunk 2 must check `get_live_orders()` BEFORE submitting
  rather than expecting the API to bounce a duplicate. The flip side is genuinely
  wanted — re-entering the same spread after a cancelled attempt is not blocked,
  which is what crash recovery needs. `FakeOrderExecution` deliberately mirrors
  this and must not be "hardened" to reject duplicates (there is a test saying so).
- **The replace contract holds**: a replace mints a NEW id, sets
  `replaces_order_id` on the new order and `replacing_order_id` on the old one, and
  leaves the old one `Cancelled` (observed 1253729 → 1253731). This was the
  assumption `FakeOrderExecution` encoded; it is now verified rather than believed.
- **Pre-market submissions book but do not work yet**: every response carried
  `tif.next_valid_session: Your order will begin working during next valid session`
  and sat at `Received`. Chunk 3 must not treat "accepted" as "working" — a
  price-walking loop that starts its clock on acknowledgement would walk an order
  that is not yet in the market. It also means a leftover test order goes live at
  09:30, before the 09:40 session: cancel and verify, do not assume.

Not to be trusted from cert: `buying_power_effect` came back `-4.54` on a $5-wide
spread whose defined risk is $435, and `fees` exceeded it. Sandbox margin numbers
are noise — size from `RiskSnapshot`, never from the ack.
