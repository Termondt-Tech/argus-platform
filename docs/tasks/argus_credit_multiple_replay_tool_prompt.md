# Argus: A Counterfactual Replay Tool for `credit_multiple`

## Priority

Not urgent. This is analysis tooling with zero deploy risk and no trading-behavior
change — it doesn't decide anything, it only makes an existing question answerable from
data that's already being collected. Safe to schedule whenever; more useful once a
handful of `entry_credit_basis = realized` sessions exist (v0.3.15+).

## Context

Whether `signal.credit_multiple = 2.0` is too tight for 0DTE can't be judged from win/loss
counts alone — a stop at multiple `m` realizes a loss of `(m-1) x credit` against a max
profit of `credit`, so the breakeven win rate is `(m-1)/m`: 50% at 2.0, 60% at 2.5, 66.7%
at 3.0, 75% at 4.0. Raising the multiple doesn't make the trade easier to win — it raises
the bar — and it only helps if a meaningful share of positions that cross 2.0x today would
have decayed back to worthless rather than continuing toward max loss. That's a question
about the *shape* of price paths, not a single number, and answering it needs to replay
real intraday behavior against alternate stop values.

That data already exists. `watchdog.check` and `watchdog.check_spent`
(`src/argus/execution/watchdog.py`, written on every tick for every open position) carry
`evaluation.to_payload()` plus `spread.credit_payload()`, which includes `cost_to_close`
and `credit_stop` — a full intraday time series per position, already sitting in the audit
log, independent of whatever multiple happened to be configured when it was recorded. See
[the third live-fire day](../decisions/2026-09-02-third-live-fire-day.md)'s "is
`credit_multiple = 2.0` right for 0DTE?" section for the full reasoning this tool is meant
to support.

## Proposed approach

Build a new read-only tool under `src/argus/tools/`, following the existing pattern in
`tools/desk.py` / `tools/mark.py` (read-only connection via `file:<db_path>?mode=ro`, CLI
entry point, `--config` flag):

1. Read all `watchdog.check` / `watchdog.check_spent` records for a given trading date,
   grouped by `external_identifier`.
2. Reconstruct, per position, the `cost_to_close` time series and `entry_credit`
   (needed to compute a hypothetical stop at other multiples:
   `hypothetical_stop = multiple x entry_credit`).
3. For a configurable sweep of candidate multiples (default: 2.0, 2.5, 3.0, 4.0), find the
   first tick (if any) where `cost_to_close >= hypothetical_stop`, and report what that
   position would have realized at that hypothetical exit versus what it actually
   realized. Reuse `tools.mark`'s existing settlement/realized-value math rather than
   reimplementing it — factor out a shared helper if that's clean, or call into it
   directly.
4. Report, per multiple: how many positions would have stopped, at what average loss,
   versus how many would have ridden to the actual outcome instead.

## Non-goals — must hold

- **Filter to `entry_credit_basis == "realized"` positions only.** CLAUDE.md explicitly
  forbids drawing `credit_multiple` conclusions from `limit`-basis positions (the basis
  in use before migration 0005 / D5, v0.3.15). The tool must enforce this filter itself,
  not leave it to the reader's discipline — segregate or exclude `limit`-basis rows,
  don't silently include them.
- The tool must not emit a recommendation or an "optimal multiple." Make the counterfactual
  visible; don't automate the decision. This needs multi-regime data before any conclusion
  is drawn (both days measured so far were trending-then-reverting, the one regime that
  makes any stop look bad) — the tool's output should be labeled accordingly.
- Genuinely read-only: `mode=ro`, never write to the DB. Never point it at the live
  `argus.db` for anything but a read — see CLAUDE.md's standing rule on this.

## Before implementing

Confirm the exact payload shape of `watchdog.check`/`watchdog.check_spent` against
current source before writing the parser — the field names/nesting referenced above are
accurate as of this review but `main` moves quickly. If `credit_payload()` or
`evaluation.to_payload()` have changed shape, adjust the parser accordingly and note the
discrepancy.

## Testing requirements

- Unit test the multiple-sweep math against a synthetic small time series with a known
  crossing point, independent of a real DB.
- A test confirming the `entry_credit_basis` filter actually excludes `limit`-basis rows
  from the output.
- A test confirming the tool never opens the DB in anything but read-only mode.

## Deliverables

- New tool under `src/argus/tools/`, following the existing `tools.*` conventions
  (read-only, `--config`, no writes).
- CLAUDE.md updated (required every session) — note the new tool in the file-structure
  section alongside the other `tools.*` entries.
- Test suite green, new/total count reported.

Confirm with Richard whether to work on a branch or on `main`, and whether to commit. Do
not push unless explicitly told to.
