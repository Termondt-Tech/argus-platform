# The sandbox reset must not clear the order ledger (2026-08-17)

*The 24h-reset stand-down deleted the order rows that would have matched orders
the broker was still reporting, and hard-halted a full trading day.*

> Settled. Found by a LIVE INCIDENT — 2026-08-14, the first morning after the
> first live-fire day. See [`README.md`](README.md) for the index.

## What happened

Friday 2026-08-14, 09:40 ET. Startup reconciliation refused the day with three
`unexpected_filled_order` discrepancies, hard-halted, and Docker's
`restart: unless-stopped` re-ran it **34 times**, paging Slack on every loop. The
day was lost.

The three orders were Argus's own, from Thursday's live-fire session:

| broker id | external identifier | status |
|---|---|---|
| 1311730 | `argus-open-20260813-call-7825-7830` | Filled |
| 1311952 | `argus-close-20260813-call-7825-7830` | Filled |
| 1311956 | `argus-open-20260813-call-7835-7840` | Filled |

## The mechanism: two windows that are not the same window

The reset stand-down and the broker's order endpoint measure different things,
and the gap between them is the whole defect.

- **`sandbox_reset_decision` keys on a trading-DAY rollover** in ET — deliberately,
  because that is what a day means everywhere else in Argus (`clock.py`), and an
  elapsed-time threshold would mean guessing at a reset schedule tastytrade does
  not publish.
- **`get_live_orders()` reaches back a rolling 24 HOURS.** This was never unknown;
  `reconcile_orders`' own docstring says so, and a test in
  `test_order_reconcile.py` covers "yesterday afternoon's rejected order."

Thursday's fills landed at 14:00:08, 14:21:58 and 14:22:08 UTC. Friday's
reconcile ran at 13:40:02 UTC — **23h18m to 23h40m later, inside the lookback**.
So the sequence was:

1. `_clear_ledger_if_reset` → `trading_day_rolled_over` → `clear_ledger()` deleted
   8 orders and 2 positions.
2. Positions compared clean: cert HAD wiped positions, and the ledger's were now
   gone too.
3. `get_live_orders()` returned Thursday's three filled orders. `known_orders()`
   returned ∅ — **because step 1 had just deleted them.**
4. `reconcile_orders` classified all three FILLED → `unexpected_filled_order`,
   `ok=False` → sandbox → hard halt.

**The stand-down manufactured the mismatch it exists to prevent.** Only 3 of the
8 cleared orders surfaced because the other 5 were cancelled walk steps, and
DEAD correctly is not a discrepancy — that half of `reconcile_orders` worked
exactly as designed.

## What was actually wrong: the documented reset is narrower than documented

`reconcile.py` cited tastytrade's own documentation (2026-07-30): the sandbox
account is reset every 24 hours, "trades, transactions, positions and balances"
all wiped. Measured against cert on 2026-08-14, that is **true of positions and
false of the order record** — the broker reported 0 positions and three live
orders in the same breath. The reset assumption was applied to a table the reset
does not reach.

## The rule

`clear_ledger` is now **`clear_reset_positions`**: it deletes `positions` and
leaves `orders`/`order_legs` in place. The Tier 1 `reconcile.sandbox_reset` record
still reports BOTH counts, because its job is to say what the ledger looked like
when the reset was assumed, not only which half was removed.

Why retention is the safe direction, point by point:

- **It can only remove false halts, never mask a real one.** `reconcile_orders`
  iterates BROKER orders and flags those absent from the ledger. Retaining rows
  shrinks the unknown set only by ids Argus genuinely placed. A foreign order is
  still flagged — covered by
  `test_an_order_that_is_NOT_argus_own_still_halts_after_a_reset`.
- **No phantom positions for the watchdog.** `_pair_spreads` gates every
  reconstructed spread on a non-zero `positions` row (`if short_held == 0 and
  long_held == 0: continue`), so an order row whose position is gone rebuilds
  nothing. This was the one hazard retention could plausibly have introduced, and
  it has its own test.
- **The atomicity argument pointed the other way all along.** `clear_ledger`'s
  docstring justified one transaction because "`positions` without `orders` is a
  held position no order accounts for." The reverse — an order whose position is
  gone — is simply what every closed position looks like.
- **The duplicate-submission guard is unaffected.** It asks the BROKER
  (`get_live_orders()`), never the local ledger, precisely because the scenario it
  exists for is one where local state was lost.
- **The same-day crash protection survives.** `RESET_LEDGER_WRITTEN_TODAY` reads
  `newest_trading_date` across both tables; with orders retained, an order
  submitted today still refuses the clear and halts.
- **Live is untouched.** The clear is gated on `environment == "sandbox"` and
  always was.

## Known gap, recorded rather than guessed at

An order of Argus's own that the broker still reports as **WORKING** from a
previous trading day is now "known" and passes the order reconcile, where before
the ledger wipe would have flagged it. That coverage was accidental — it came
from deleting the ledger, not from detecting a stale working order — but it is
real coverage that this change removes.

Not built for now, because cert has never left an order working overnight (all
five of Thursday's non-filled orders went terminal within seconds) and building
for an unobserved case would be the guess the rule forbids. If cert ever does
strand a working order overnight, the position reconcile catches its fill the
next morning as `unexpected_at_broker`. **The fix, if it is ever needed, is a
past-trading-date check on WORKING orders specifically — not a return to wiping
the ledger.**

## What this says about sandbox generally

Cert proved an API contract wrong that was taken from vendor documentation rather
than measurement, and it took a live-fire day plus a lost trading day to surface.
The reset stand-down is the one place a safety rail is deliberately allowed to
stand down, which makes every assumption inside it load-bearing. This is the
second time it has produced a false halt.
