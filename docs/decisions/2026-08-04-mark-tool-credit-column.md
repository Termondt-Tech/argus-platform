# The mark tool's credit column (resolved 2026-08-04)

*Why ~18% of marked rows showed a credit that did not match their own P&L.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


A six-day backlog item — ~18% of reconciled rows (27 of 151) showed a `credit`
that did not match their own displayed P&L, always by exactly $0.005 per contract
— root-caused and fixed. **It was display-only.** Confirmed by tracing the value,
not assumed: order pricing goes through `zerobid.round_to_tick` (`Decimal`,
`ROUND_HALF_UP`, normalized to 6dp first), which never saw the defect, and the
`.2f` in question existed in exactly one place on this path,
`tools/mark.py::_print_table`. `shadow.would_submit` stores `credit` full-precision
and `marker._decode` reads it back full-precision, which is why every daily P&L
total reconciled exactly while the credit column did not.

- **A credit is a HALF-TICK quantity, and that is the whole mechanism.** A leg's
  floored mid is `(bid + ask) / 2` on a $0.05 ladder, so it lands on a $0.025 grid,
  and so does the net of two legs. Roughly half of all credits are therefore an ODD
  multiple of $0.025 — 0.525, 0.575, 0.625 — which is exactly the tie two-decimal
  rounding has to break. (The handoff called this "quarter-cent"; it is precisely
  half a tick, $0.025.)
- **The tie is broken by float representation error, so identical values printed
  differently.** `short 0.60/0.70, long 0.10/0.15` → `0.5249999999999999` → "0.52";
  `short 0.65/0.70, long 0.10/0.20` → `0.525` (whose nearest float is a hair ABOVE)
  → "0.53". That is the 2026-07-30 rank-17/rank-19 pair, same run, one row apart,
  reproduced end-to-end through the real tool against a scratch DB.
- **The fix is Decimal AND a third decimal place, and the second half is the one
  that matters.** `ROUND_HALF_UP` alone would have made 0.525 print as "0.53"
  consistently — deterministic, and still unable to reconcile: 0.53 × 4 × 100 is
  $212 against a P&L of $210. The column is read by multiplying it against P&L by
  hand, so it now prints the value it actually has. Three decimals is exact for any
  real quote (penny-quoted legs give a $0.005 grid, nickel-quoted $0.025).
- **Scope stayed at the price columns.** `credit` and `liab` (same defect class,
  same row, and `(credit − liab) × contracts × 100` is the partial-row form of the
  same check) moved to 3dp; P&L stays in dollars at 2dp but goes through the same
  rounding so a total can never depend on how a float landed. Verified against real
  data: 2026-07-22 renders byte-identical apart from the widened price columns —
  **no P&L figure or total changed**.
- **Two same-class sites deliberately NOT touched**, both different surfaces:
  `status.py::_fmt_money` (account NLV / day P&L — broker dollars, not half-tick
  prices) and `tools/order_smoke.py`'s diagnostic credit print. Flagged, not fixed.
- The pre-fix behaviour is PINNED in `test_mark_display.py`, not just described —
  the two floats, the two `.2f` strings they used to produce, and the reconciliation
  identity that now holds. The credits are built through `OptionQuote.floored_mid`
  rather than hand-written, so the test follows the real arithmetic if it changes.

**Still outstanding:** the five hand-reconciled dates (2026-07-28, 07-29, 07-30,
07-31, 08-03) live on the droplet, not in the local `argus.db` (which ends
2026-07-23), so they have NOT been re-run. Do that on the droplet with `--dry-run`
(read-only, safe mid-session) once this deploys.
