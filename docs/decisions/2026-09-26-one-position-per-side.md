# One open position per side: the side-balance rule

**Status: DECIDED 2026-09-26 (Richard, "package B"); BUILT the same day for v0.5.2, DEPLOYED in v0.5.2 (Mira, 2026-09-26)** (config sha `8322ab49c926`; **no trade has run under it yet — the first live session is 2026-09-28**). Design: [multi-spread side balance](../designs/2026-09-26-multi-spread-side-balance.md).
**Review 2026-09-27 ([findings](../reviews/2026-09-27-v0.5.2-mira.md)): two should-fix
findings, FIXED on `claude/intelligent-carson-zrz1il` (`src/` + tests only, image-only, NOT
yet tagged or deployed).** (2) Counting an unreadable side "against both" was neutral, not
fail-closed; unknowns are now their own count, and the rule assumes the worst side for them.
(3) The design's AST pin on trade-idea keying is built; two design deviations are recorded,
and **DECIDED 2026-09-27 (Richard): the single symmetric `max_side_imbalance` is accepted, so
a put lean is a decision record plus a code + config release, never a config flip; and no
`would_lean` field is built, because its counterfactual is derived from `refused`
`signal.candidate` rows** ([design §8.4](../designs/2026-09-26-multi-spread-side-balance.md)).
Evidence: [the two-slot research](../../RESULTS.md),
[the exploration](../../RESULTS.md) §3 and its
`config_grid.py`.

## The decision

With two slots, the second spread goes on **the other side**. Stated generally, so it
still means something at more slots:

- **Admissibility.** An entry is allowed only if afterwards
  `|puts − calls| <= risk.max_side_imbalance`.
- **Priority.** Among the sides that qualify on a tick, the one leaving the book more
  balanced goes first. Ties go in `signal.sides` order, which puts the put first.
- **Configuration.** `risk.max_side_imbalance = 1` at two slots means at most one open
  spread per side. `risk.side_balance` switches the rule, and `false` is the
  pre-v0.5.2 entry path exactly.

**Richard's put preference survives as the tie-break, and only as the tie-break.** A
put-leaning band was measured and not adopted (design §4–§5): there is no measured
put-side edge, the skew prices the down-tail, and the lean costs tail.

## Why

**Risk.** Same-side positions move together (+0.62 to +0.69) and opposite-side ones
offset each other (−0.26 to −0.59), so book variance rises with the square of the
imbalance (design §3.2). At v0.5.2's caps the replay has:

- Day SD falling from 107 to 100.
- The worst day improving from −$258 to −$158, under the strike-touch stop, 24
  sessions.
- The average −$10 to −$19 a day as a main effect. That is not significant, and it
  is partly an artifact: the replay has to impute the call side.
- In the path models, one per side has equal or better mean in three of four models
  and a better CVaR 1% in all of them (design §5.2).

**The book it replaces was accidental.** Without the rule the second slot takes
whatever the strategy returns first. The result is a put and a call on about 70% of
days, and a chained same-side pair on the rest; the pairs carry most of the tail.

## What was built

| Piece | Where | What it does |
|---|---|---|
| The rule | `risk/side_balance.py` (new, pure) | The counting formula, admissibility and ranking, in one place |
| Per-side counts | `RiskSnapshot.put_spread_count` / `call_spread_count` / `unknown_side_spread_count` | `open_spread_equivalent`'s own formula applied per side: `max(ceil(broker legs / 2), ledger trade ideas) + working opening orders`; unreadable sides as their own bound (2026-09-27) |
| Broker legs by right | `risk/snapshot.py`, via `occ.occ_right` | Read off the account snapshot the builder already holds. `occ_right` is new; see its docstring for why the right is now exposed |
| Ledger and working counts | `PositionWatchdog.open_spreads_by_side`, `working_orders.open_working_spreads_by_side` | Side read from the identifier Argus minted (`working_orders.side_of_identifier`), never from the broker's relabelled leg actions |
| The choice | `orchestrator.SignalLoop._choose` | Ranks the tick's qualifying sides and skips any the pipeline already attempted (`EntryPipeline.was_attempted`, read-only). Without that skip, a repeated put hid a qualifying call for the whole tick |
| The refusal | `PreTradeGate`: `side_imbalance_exceeded` | Enforced twice, like every size rail: the selector chooses, the gate refuses |
| The data | `StrategyResult.alternatives`, `signal.candidate.other_sides` / `.book` | The strategy evaluates every side on every tick. `book` records the counts, the rule's settings and the choice (`off` / `first` / `balance` / `refused` / `attempted`) whether or not the rule is on |

**Fail-closed everywhere a side cannot be read.** An unreadable broker leg, working
order or ledger idea is counted as `unknown` (`RiskSnapshot.unknown_side_spread_count`),
and an entry is admissible only if it stays inside the band **whatever side the unknowns
are on**: `|known imbalance after| + unknown ≤ max_side_imbalance`. The rule only ever
refuses an entry, so "unknown" must lean toward refusing, never toward a free slot.

*Corrected 2026-09-27 (review finding 2).* As built for v0.5.2 an unknown counted against
BOTH sides. That reads as fail-closed and is exactly neutral: +1 to each side leaves
`puts − calls` unchanged, so it changed no verdict, and an unknown that was really a put let
a second put open. The tests pinned the counts, never the refusal.

**Never counted by symbol alone** (CLAUDE.md's fourth landmine). A chained pair nets
its shared strike to zero at the broker; it is two from the ledger, and the higher
count wins.

## Tests and verification

- `tests/test_side_balance.py` (37 tests) covers the rule's table, the counts including
  chained pairs and unreadable symbols and identifiers, the gate, the orchestrator end
  to end through real audit records, the rule OFF being the old path exactly, the
  config, and the session wiring.
- `tests/test_other_sides_telemetry.py` (10) and one test in `test_entry.py` cover the
  rest.
- **Nine mutations of the load-bearing lines are all caught:** the band in both
  directions, the dedup skip, the rule-off path, the proposal's side, the gate's
  refusal, both fail-closed counts, and the credit switch.
- Suite: 3,063 passed, 1 skipped.
- Strategy benchmark: 0.125 ms per tick with both sides priced, against a 0.2 ms budget.

## What it does not change

The strategy's thresholds and delta source; the size rails; the welded invariant; the
loss budget and its full-width count (a netted "condor" budget is refused: exploration
§2, research §4.6); the watchdog and every exit path; single-legging (every vertical
stays one order with its own stop and flatten).

## Rollback, and what to watch

- **Rollback** is `risk.side_balance = false`, a config-only release, never with a
  position open.
- **`/status` has no Book row yet.** The counts ride `gate.decision` and
  `signal.candidate.book`; the row is a follow-up.
- **To watch in the first live week:**
  - `book.choice`: `balance` means the rule moved the second slot; `refused` means it
    held it.
  - `gate.decision` reasons containing `side_imbalance_exceeded`.
  - That no two same-side spreads are ever open at once. Any such observation is a
    defect, not a finding.
