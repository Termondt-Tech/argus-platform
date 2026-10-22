**Design only, no code.** Written so it can be picked up and built without another design question.

→ the record (private record, not published)

## The hole the first live night found

2026-09-22 opened one put spread (`20260922-put-7740-7735`, 2 contracts, 0.50 credit — the first trade ever selected on mid-implied delta) and **never closed it**: at 15:30 the flatten found no market, so it refused to invent a price and the spread expired. That's the flatten's documented modal regime on a winning day, not a malfunction — confirmed by Richard.

A 0DTE expiry produces no fill to price from, and an **OTM expiry produces no broker transaction either**, because only ITM options cash-settle. So the day would have sat `PROVISIONAL` with a blank gross indefinitely, and the Dashboard's headline Net P&L would have silently excluded **+$93.12**. The original design covered the ITM case and missed the OTM one, where the result is knowable but unreported.

## Decisions (both Richard's, both the recommendation)

| | |
|---|---|
| **Price source** | The day's **last recorded spot** from the chain (`watchdog.check` / `check_spent`). No network, and any past day re-renders to the identical number — `--from-feed` cannot do that. |
| **Headline P&L** | A settlement-valued day **counts**, labelled, with its own KPI and a `gross_basis` column. Otherwise a winning month is understated every time the flatten can't find a market. |

Plus, decided and recorded: a **persistent** `/etc/argus/settlements.csv` override (the ledger rebuilds every day, so a one-off value would be silently replaced and a hand edit in the Sheet overwritten); a 15-minute staleness bound on the recorded spot, refused rather than guessed; `PROVISIONAL` regains its meaning as "valued by neither fills nor settlement".

## The load-bearing rule

**Reconciliation keeps comparing FILLS ONLY.** A settlement-derived number in `broker_gross`'s comparison would mark every such day `DIFF` for a difference that is *correct*, since the broker has no row for an OTM expiry. `days.gross` stays fills-only; the settlement value rides in its own column. A test asserts it.

## What stays open, deliberately

An ITM expiry **does** post a cash settlement, and its type/subtype strings have never been seen — the first live read returned only ordinary `Trade` rows. Today's behaviour is kept: a row with no `order_id` is `unmatched_broker` → `DIFF`, loud. The record names the trigger for closing it (capture the first real row as a fixture, then pair it with the settlement-valued trade) and says **not to guess the strings** — that assumption is exactly what the same live read overturned for `is_estimated_fee`.

## Ready to build

- exact new columns, appended so no existing sheet column moves
- the two functions to reuse (`settlement_liability`, `classify`) and an AST rail that no settlement math is re-implemented
- 11 owed tests, including `partial`, `max_loss`, a call spread, a half-day close, and the malformed-override case
- an acceptance check against the real spreadsheet: Net P&L **−$515.20**, "Of which valued at settlement" **$100.00**, "Days with an unknown result" **0**, no error cell

🤖 Generated with [Claude Code](https://claude.com/claude-code)
