# Overlapping verticals break a symbol-keyed position model (2026-08-17)

*Two concurrent spreads sharing a strike erased each other. Both went unwatched,
and the concurrency rail stopped bounding anything.*

> Settled. Found by a LIVE INCIDENT — 2026-08-17, the first session with two
> spreads open at once. See [`README.md`](README.md) for the index.

## What happened

The first `watchdog_unwatchable_position` page arrived minutes after the second
entry. Two spreads were open:

| order | candidate | short | long |
|---|---|---|---|
| 1344929 | `20260817-put-7750-7745` | 7750 | 7745 |
| 1345003 | `20260817-put-7755-7750` | 7755 | **7750** |

One spread's SHORT leg is the other's LONG leg. `positions` is keyed by symbol
(PRIMARY KEY) and holds only the NET, and `_apply_position` **deletes** a row that
nets flat — so selling 2× 7750 and buying 2× 7750 removed the 7750 row entirely.

Reassembly then walked newest-first and lost both:

1. `1345003`: short 7755 held −2 ✓, long 7750 held **0** → the `long_held <= 0`
   guard fired → malformed, and it `claimed` both symbols.
2. `1344929`: its short leg is 7750, already claimed, so it was skipped silently as
   "a newer opening order already accounts for this position."
3. 7745 was held +2 and claimed by nobody → `unattributed_symbols`.

**Neither spread was watched.** A live position — net short 7755 / long 7745, two
lots, 10 wide — had no virtual stop being evaluated on it.

## The second failure, which is worse

The same netting defeats the concurrency rail. `RiskSnapshot.open_spread_equivalent`
was `ceil(broker_legs / 2)`, and the broker nets the shared strike to zero exactly
as `positions` does. So the two put spreads presented as **2 legs → 1 spread**, the
gate saw a free slot, and a third spread (`20260817-call-7790-7795`) was allowed.

Three spreads open against `max_concurrent_positions = 2`, ~2850 of combined max
loss against a `daily_max_loss` of 2500 — the standing invariant
`max_concurrent × max_loss_per_position ≤ daily_max_loss` breached in practice.

And it generalizes badly. Chain them — 7755/7750, 7750/7745, 7745/7740 — and every
interior strike nets out: **k spreads present as 2 legs however large k is.** A
leg-derived count reads "1 spread" forever, so the limit stops bounding anything
and exposure grows without bound. Nothing about the strategy prevents the chain:
`signal.width = 5.0` against SPX's 5-point near-the-money ladder makes consecutive
candidates chain *by construction*. This is the common case for two same-side
spreads, not an edge case.

## Why it took until now

`max_concurrent_positions` only became genuinely 2 spreads on 2026-08-13, when the
gate stopped counting legs. That day's two spreads shared no strike and were never
even concurrent — the first closed two seconds before the second opened. 2026-08-14
was lost to the reconciliation halt. **2026-08-17 was the first session in which two
spreads were ever open simultaneously**, and it broke on the first one.

## The rule

**Spread holdings are attributed per TRADE IDEA, never by net symbol.**

`order_legs.applied_quantity` already records what filled per order per leg, so no
migration was needed — the attribution existed, nothing read it. Reassembly now
groups every order (open AND close) by trade idea and sums its own legs. Two ideas
sharing a strike cannot cancel each other because they are never summed together.

- The group key is `candidate_id`, falling back to the candidate parsed out of
  `external_identifier` (NOT NULL, deterministically `{prefix}{direction}-{candidate}`).
  A key that cannot be established reports the order rather than guessing — grouping
  unrelated orders would merge two positions into one nonsense spread.
- Re-entry after a close still collapses correctly: opens and closes sum inside one
  group, priced from the newest opening order.
- **The gate takes the HIGHER of broker-derived and ledger-derived counts.** Each
  catches what the other cannot: the broker sees a position the ledger never booked
  (2026-08-11), the ledger sees overlap the broker structurally cannot report. The
  ledger can only ever TIGHTEN the limit — it never hands back a slot the broker
  says is taken.

## The invariant that keeps attribution honest

Per-idea attribution decides *which* idea holds what. It must never invent a
holding. So the sum across every reassembled spread must equal `positions` symbol
by symbol, and where it does not, the spreads touching that symbol are reported
instead of watched.

This is load-bearing, and it is what stops the change from reintroducing a hazard
v0.3.2 was careful about: an order ledger retained across a sandbox reset describes
spreads whose `positions` rows are gone, and without the cross-check they would be
watched as phantoms. The overlapping case passes it cleanly — 7755/7750 and
7750/7745 sum to zero on 7750, which is exactly what `positions` holds.

**The v0.3.2 reset changed with it.** It now clears `positions` AND `order_legs`,
keeping only the `orders` identity rows. That is precisely what v0.3.2 needed —
`known_orders()` reads `orders` alone, with no join to `order_legs` — while leaving
no applied-leg record from which to rebuild yesterday's spreads.

## The 09:05 alert was a different fault, and it exposed two more

Distinct from everything above: `"1 applied leg(s), expected 2"` fired while the
order was mid-fill. The two legs of ONE multi-leg order book as separate fill
events (~0.25s apart, measured 2026-08-13), and `on_filled` calls
`request_refresh()` the moment the first lands — so the watchdog aims straight at
the window where a spread legitimately has one applied leg.

Investigating it surfaced a second, larger problem on the same path: **it had no
escalation at all.** Tier 1 plus an alert on every refresh while the condition
persisted — 66 Tier 1 records and a page every 15s for one unchanging fact, with
the Slack suppressor doing all the work of making it survivable.

Both are fixed:

- **Episodes are signature-keyed.** One page per distinct problem, Tier 2 while it
  persists, and an immediate page if the signature changes — so suppression can
  never hide a genuinely new fault. Recovery is Tier 1 as well, pairing the page:
  an operator told a position is unwatched must be told when it is watched again.
- **`book_settle_s` governs both post-fill windows.** They are the same phenomenon
  — the ledger catching up right after a fill — and a second key meaning the same
  thing would only drift from this one. No config change; the value is unchanged.

The asymmetry from 2026-08-13 is preserved exactly: the directions that mean a real
position may be unwatched — `held_but_unbooked`, and an `unattributed` row no order
explains — have no pending second leg that would resolve them, so waiting only
delays the page. They still fire on the first observation.
