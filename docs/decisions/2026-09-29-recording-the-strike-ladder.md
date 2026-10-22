# Recording the strike ladder (2026-09-29)

**Built 2026-09-29, not yet deployed. Telemetry only, image-only:** no config key and no
decision reads it. It is the objective audit's P9.

## What was missing

`signal.candidate` records the pair the selector chose, and `signal.none` its near miss.
**No other strike's quote was ever on the record**, so three questions could be answered
only through model marks, whose median error (0.04–0.05) is 8–10% of a 0.55 credit, the size
of the effects being measured:

- **`target_short_delta`.** At `max_loss_per_position` 475 the width is pinned at 5, so
  delta is the one lever on credit per lot, and so on the share of it that fees take.
- **`min_credit`.** Would the next strike in have paid (the audit's P10)?
- **What the 15:30 flatten could actually buy.** Whether a far out-of-the-money spread's
  real mid clears a tick. That decides whether the replays' flatten model or the
  "sends nothing" model is right, a $6–10/day swing in how the book's mean reads
  ([due diligence](../../RESULTS.md) §2).

## What it records

**Tier 2 `signal.ladder`, once per ET minute** (the first tick of each), all session. That
includes the flatten half-hour and blackout days.

Per side, every tradeable **out-of-the-money** strike relative to the selector's forward,
nearest first, up to **40**. Each field is a columnar array:

| Field | Meaning |
|---|---|
| `strike` | the strike |
| `bid`, `ask` | the quote |
| `mid_delta`, `mid_iv` | the mid-implied delta and vol, `null` where the mid does not invert |
| `otm_strikes` | how many out-of-the-money strikes the side had |
| `truncated` | how many were left off by the cap |

Each row also carries spot, forward and its source, minutes to settlement, `atm_iv`, and
`captured_at`.

- **The mid deltas come from the selector's own functions** (`strategy._mid_pricing`,
  `_solve_side`), imported rather than re-derived, and tested equal strike by strike.
- **The legs are the snapshot's own**, which `live_chain` already admits as quote-fresh.

**Size:** about 6.5 KB a row on an 800-leg chain, built in about 5 ms. Across 380 rows a
session that is about 2.5 MB, against about 20 MB a session for the live DB today, and
under 1% of the write rate. The cap and cadence are module constants (not config): they
bound a record, not a decision.

## Rules (pinned)

- **Never costs a decision.** It is written before the strategy runs, and `maybe_record`
  swallows every exception from building the row. The minute is claimed first, so a failure
  logs once and is not retried every tick.
- **Written, never read.** In `src/` outside `tools/`:
  - only the orchestrator imports the module;
  - it calls only `maybe_record` and discards the result;
  - the category string lives in the ladder module alone.
- **Every number goes through `payload_number`.** A hostile quote or spot cannot refuse the
  row. The existing hostile-input tests now also assert that the minute's ladder row survives.
- **Tests:** `tests/test_ladder_telemetry.py` (13). Four mutations, all caught: no minute
  gate, no exception guard, the feed delta instead of the mid, and a raw bid.

## Not in this change

- **No reader tool yet.** The first uses are:
  - scoring alternative deltas held to settlement and under the touch stop;
  - the flatten's real 15:30 prices on the held strikes;
  - `min_credit`'s next strike in.

  Each gets a research script when enough sessions exist (≈ 30 for a first read).
- **No change to selection, pricing, sizing or any rail.**
