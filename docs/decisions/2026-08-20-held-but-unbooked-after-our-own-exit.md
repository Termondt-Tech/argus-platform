# `held_but_unbooked` after our own exit (2026-08-20)

*Post-fill propagation lag runs in BOTH directions. `book_settle_s` was calibrated
on an entries-only day and wired to one of them. The exit direction pages instantly
for the same ordinary physics — and the fix is not a second timer, because a timer
in this direction would suppress the one condition this check exists to report.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

## What was observed

The first intraday stop since v0.3.5. The exit filled; 1.215s later the
book-agreement check paged:

```
watchdog.book_disagreement
{ "broker_legs": 2, "ledger_legs": 0, "direction": "held_but_unbooked" }
```

It cleared on its own 6.012s later. Nothing was wrong: Argus had just closed the
position, the ledger knew, the broker's book had not caught up.

## Mechanism

Exactly the mechanism [Fix B](2026-08-13-live-fire-sandbox-pass.md) was built for,
running the other way.

| | Entry | Exit |
|---|---|---|
| Argus's ledger | `positions` row appears on the fill | row is DELETED on the fill |
| The broker's book | trails by ~4–5s | trails by ~6s |
| Resulting direction | `booked_but_absent` (broker < ledger) | `held_but_unbooked` (broker > ledger) |
| Before this fix | `book_settle_s` allowance | **pages instantly** |

`store._apply_position` DELETEs a `positions` row that nets to flat, and
`FillConsumer` calls `request_refresh()` the moment the closing fill lands — so the
watchdog is aimed directly at the window where its own exit has left the ledger and
not yet left the broker's book.

Fix B was measured on a day with **entries only**. Its one-direction scope was
correct for what had been observed and incomplete for what had not: the first
intraday stop since is what exercised the other half.

## The discriminator, and why not a timer

`held_but_unbooked` means *a position may be held with no virtual stop being
evaluated* — the exact gap
[the broker-agreement check](2026-08-11-unwatched-position-gap.md) exists to catch,
and the reason it was deliberately given no allowance. **A blanket
`book_settle_s`-style window in this direction is rejected**: it would suppress a
genuinely orphaned position for 20 seconds because a clock said so, which trades
the one page that matters for quieter logs.

What distinguishes today's case from a real orphan is not elapsed time. It is that
Argus itself had just closed those legs. So the allowance is keyed off **evidence**:

- `PositionWatchdog.note_fills` records the OCC symbol of every leg one of Argus's
  own **closing** fills took to **flat** (`action.closes and position_after == 0`)
  — precisely the set that left `positions` and can therefore make the ledger read
  lower than the broker's trailing book.
- `_book_allowance_elapsed` suppresses a `held_but_unbooked` disagreement only when
  `broker_legs - ledger_legs` is covered by legs in that set. The suppressed Tier 2
  record names them (`closing_fill_legs`), so the reason is in the trail.
- **Anything not covered pages immediately and unconditionally**, exactly as before.
  A partial close (row survives, count unmoved) buys nothing. An opening fill buys
  nothing. One closed leg does not excuse two unbooked ones.

The evidence's own lifetime is the window. `_closing_fill_cover` prunes at
`book_settle_s` — the same knob, because it is the same lag, and a second key
meaning "how long does the broker's book trail a fill" would only drift from the
first (the rule already stated for the 2026-08-17 unwatchable window).

## Wiring

`FillConsumer.consume` now returns the `AppliedFill`s it booked (empty tuple for
the common status-change repeat), and `session.on_order` passes them to
`note_fills` **before** `request_refresh()` — the refresh is what runs the
comparison, so recording the evidence after it would race the check it informs.

Returned rather than pushed through a new callback: `on_order` already holds the
late-bound watchdog reference for `request_refresh`, and one seam is enough.

## Order of checks

The cheap in-memory discriminator runs first; the REST round trip
(`_cache_is_over_reporting`, from
[the ghost-positions fix](2026-08-19-account-cache-ghost-positions.md)) still runs
only on the paging edge. Both are suppressors of the same page, from opposite
sides: this one says *the broker's book is trailing our own exit*, that one says
*the account cache invented a leg the broker does not hold*. Neither weakens the
other — a disagreement that is neither still pages on sight.

## Tests

`tests/test_watchdog.py` pins today's case (fires at 1.215s, settles at 6.012s,
`watchdog.book_settled` carrying the direction) and four negatives that must still
page instantly: no closing fill at all, an opening fill, a partial close, and an
excess larger than the legs actually closed. The pre-existing
`test_held_but_unbooked_pages_on_the_first_observation_regardless` is unchanged and
still passes — the default posture did not move.
