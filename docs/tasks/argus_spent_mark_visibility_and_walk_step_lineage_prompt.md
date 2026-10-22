# Argus: A Spent Position Is Invisible To The Read-Only Tools, And A Walk's `steps` May Count A Reprice That Never Landed

## Priority

**Two separable items, both small, both from the 2026-08-31 flatten pass (finding
5). Neither is a safety rail and neither touches the runtime.** Item A is a
reporting defect in the read-only tools with one real consequence — in
`tools.evaluability` it is a **denominator error**, not a display gap, so the
instrument can report a day as INCONCLUSIVE that it in fact measured. Item B is a
data-lineage question that must be **established as fact before anything is
changed**, because if it resolves the way the evidence points it invalidates a
measurement two other decisions lean on.

Item A may be implemented once the design questions below are answered. **Item B is
investigate-and-report only** — propose a fix, do not change `WalkResult`'s
semantics without Richard's sign-off, because `steps` is a number other records
have already been derived from.

→ [the flatten's first fire, finding 5](../decisions/2026-08-31-flatten-first-fire-pass.md)

## Item A — `watchdog.check_spent` is invisible to the read-only tools

v0.3.13's P1 changed what a spent position emits. Before it, a position whose exit
attempt was spent got no evaluation at all; now it is evaluated every tick and
recorded — but under a **different category**:

- `watchdog.check` — an ordinary watched position
- `watchdog.check_spent` — a position whose exit attempt is spent, carrying the same
  mark (`cost_to_close`, `credit_stop`, `spot`, `still_triggered`) plus `exit_outcome`

Neither tool knows the second category exists.

**`tools/desk.py`** (`CHECK = "watchdog.check"`, line 60; the `OPEN / HELD` mark at
line 127, *"The LAST `watchdog.check` per position on the date"*). On 2026-08-31 it
reported the put spread marked `@15:29:51` — nine seconds before the flatten fired —
because every subsequent look was a `check_spent`. The watchdog evaluated that
position **1,175 more times** after the timestamp desk printed.

**`tools/evaluability.py`** (`CHECK = "watchdog.check"`, line 54) is the worse of the
two, because there the count is a **denominator**:

```python
ticks = counts.get(CHECK, (0, 0))[1]      # line 242
```

That figure produces the tool's headline `-> 0.0% of ticks blind (3 of 37,965)`, and
1,175 spent-position ticks are missing from it. Worse, line 307 renders
`INCONCLUSIVE — no watchdog.check records on this date` when the count is zero — so a
day on which every position went spent early would be reported as unmeasured even
though the watchdog evaluated throughout. That is the instrument that
`docs/decisions/2026-08-13-live-fire-sandbox-pass.md`'s Fix D was closed against, and
the one `argus.toml`'s `[audit] synchronous` comment tells you to re-measure the write
rate with.

### What to investigate before writing code

**A1. Merge, or distinguish?** A spent position's mark is not the same object as an
ordinary one: it is a price **nothing automated will act on**. Desk silently folding
`check_spent` into the OPEN / HELD table would fix the stale timestamp and lose that
distinction. Decide and argue it — a column, a flag, a separate section, or a merge
with the `exit_outcome` shown. Prefer whatever an operator reading the table at
16:05 needs in order to not misread a held position as a managed one.

**A2. `cost_to_close` is `None` on most spent ticks, and that is load-bearing.** P1's
own rule is that `None` renders as `unpriced`, never as a number, because *"a `0.0` on
a row a human might act on by hand would be the wrong kind of value, not a missing
one."* On 2026-08-31 the put's spent marks were `None` far more often than not.
Whatever desk does must inherit that rule — and note the LAST record is not
necessarily the last PRICEABLE one, so "the last mark" and "the last usable mark"
are different queries and you should say which one the table wants.

**A3. Is the evaluability denominator the only arithmetic affected?** Check every
aggregate that tool computes, not just the headline percentage — episode counts,
gap-between-episodes, the write-rate split by tier. Report which are affected and
which are not. Also check whether `tools.mark` / `shadow.marker` read `watchdog.check`
anywhere; `tools.desk` documents that it reuses marker's fill reconstruction, so
confirm whether the blindness reaches further than these two files.

**A4. Is the right fix in the readers, or in what the watchdog records?** Default to
the readers. `watchdog.check_spent` being its own category is deliberate — it is what
makes a spent position greppable and countable — and collapsing it back into
`watchdog.check` to make a tool simpler would destroy the signal P1 was built to
create. If you conclude the record is genuinely wrong rather than the readers, say so
explicitly and stop, because that is a runtime change and this task is not scoped for
one.

## Item B — `final_limit_price` and `steps` may describe a reprice that never landed

**Investigate and report. Do not change `WalkResult` without sign-off.**

On 2026-08-31 the put spread produced three numbers that should be reconcilable and
are not:

| number | value | source |
|---|---|---|
| fill-derived credit (desk CREDIT) | 0.55 | `order.fill`, via `shadow.marker.read_executions` |
| `entry_credit` (on `/status`, `watchdog.check`) | 0.55 | `head["limit_price"]`, the NEWEST FILLED opening order (`store.py:871`) |
| `final_limit_price` (desk LIMIT) | **0.50** | `state.sent.limit_price` (`loop.py:1282`) |

**The first two agreeing is not a defect** — a limit order normally fills at its
limit, and `entry_credit` is documented as a limit and is one. An earlier reading of
this day called that pair a contradiction; it is not, and the decision record now
carries the correction. The odd number is the third.

`WalkResult.final_limit_price` is *the limit the walk believes it last sent*. Desk
prints it under a column headed `LIMIT` in a table headed
`EXECUTION QUALITY (steps = reprices needed to get filled)`.

**The leading hypothesis**, from the same day's counters: `order.replace_attempt: 1`
and `order.replace_failed: 1`, with the put the only entry carrying `steps: 1` (the
call filled at `steps: 0`). If the reprice 0.55 → 0.50 failed and the original 0.55
order subsequently filled, then `final_limit_price` reports a rung that was **never
live at the broker** and `steps` counts a concession that never happened.

### What to investigate

**B1. Establish the mechanism as fact, not inference.** Read `PriceWalkingLoop`'s
replace path in full and determine exactly when `state.sent` is updated relative to
the broker acknowledging a replace, and what happens to `state.steps` when
`order.replace_failed` is written. The answer is in the code and does not need the
database. If `state.sent` is advanced optimistically before the broker confirms, that
is the whole finding.

**B2. Which other numbers are derived from `steps`?** At minimum:

- `tools.desk`'s EXECUTION QUALITY table, whose column header asserts `steps` means
  *"reprices needed to get filled"*.
- **`argus.toml`'s `execution.walk_source` string**, which records *"MEASURED against
  Argus's own shadow record, 84% of candidates (63/75, median credit 0.525) get
  exactly ONE $0.05 reprice before the floor, 16% get two."* If `steps` counts failed
  replaces, that measurement is not measuring what its own sentence claims.
- `WalkOutcome.DRIFTED` / `WALKED_AWAY` reasoning anywhere it reads a step count.

**This is the item's real weight.** That 84% figure already needs re-deriving because
of the 2026-08-31 exit-ladder finding — the floor arithmetic means a 0.50–0.55 credit
gets exactly one rung by construction, so the measurement may be describing the
ladder rather than the market. If `steps` *also* counts reprices that never landed,
that is a **second, independent** reason, and the two compound. Say so plainly in the
report; do not re-tune anything.

**B3. Is `final_limit_price` misnamed, mis-populated, or correctly reporting a
different thing than desk assumes?** All three are live possibilities and they have
different fixes: rename the field, populate it from the acknowledged limit, or change
what desk prints and how the column is headed. Recommend one, with the reasoning.
Note that `entry.py:706` also logs it (`"entry walked away for %s at %s"`), so a
change of meaning has a second reader.

**B4. Does the same question apply to the EXIT walk?** `final_limit_price` is
populated in the shared `_finish`, so exits carry it too, and the 2026-08-31 exits
both reported `walked_away after 0 step(s) at -0.05`. Report whether the exit path
can reach the same state. Do not fix the exit ladder here — that is
[its own task](argus_exit_ladder_near_tick_prompt.md), already designed and built.

## Constraints

- **The tools are READ-ONLY and must stay that way.** `tools.desk` and
  `tools.evaluability` are documented as safe to run mid-session with no writer lock.
  Nothing here may introduce a write, a lock, or a broker call.
- **NEVER run any tool against `argus.db` without `--config`.** All development,
  testing and manual verification uses `argus.dev.toml` (`--config argus.dev.toml`),
  which writes to the gitignored `dev/`. Reads for analysis are
  `file:argus.db?mode=ro`. This is the rule that exists because a bare
  `python -m argus` once cost ~2,000 records of live signal data.
- **Do not change `OpenSpread.entry_credit`'s basis.** It is `head["limit_price"]`
  deliberately, it is documented as low by a measured 9–18%, and correcting it means
  capturing `FillInfo.fill_price` durably — a column and a migration, a known-deferred
  item. This task may CLARIFY what the field is; it may not change what it reads.
  → `execution/store.py:108-128`
- **Do not change what the watchdog records** (see A4). The fix for a reader's
  blindness belongs in the reader.
- **Do not re-tune `entry_walk_away_pct`, `exit_walk_away_pct`, or the `walk_source`
  numbers.** Item B may conclude that a measurement is unsound; saying so is the
  deliverable, replacing it is not.

## Testing

`tests/test_desk.py` and `tests/test_evaluability_tool.py` both exist — extend them,
do not fork.

- Desk: a position with `watchdog.check` records up to T1 and `watchdog.check_spent`
  records up to T2 > T1 reports the T2 mark, not T1, and whatever distinction A1
  settles on is visible in the rendered output.
- Desk: a spent position whose last mark has `cost_to_close: None` renders `unpriced`
  and never `0.00`. Assert on the rendered string — this is a display rule and a
  display test.
- Evaluability: the blind percentage's denominator includes both categories. A
  fixture where every tick is `check_spent` must **not** report INCONCLUSIVE.
- Evaluability: a day with no records of either category still reports INCONCLUSIVE,
  unchanged.
- Item B: if any change to `WalkResult` is approved, a test that a FAILED replace
  leaves `final_limit_price` and `steps` describing what the broker actually
  acknowledged. Until then, a test is not owed — the deliverable is a report.

## Deferred / explicitly out of scope

- The exit ladder's zero range near the tick, and `CROSS_UNPRICEABLE` spending the
  attempt — findings 1 and 2, designed, signed off and built 2026-08-31.
  → [the exit ladder](../decisions/2026-08-31-exit-ladder-near-tick.md)
- An exit-scoped staleness bound (finding 3). Report-only there, out of scope here,
  and still never to be fixed by raising `staleness.market_data_max_age_s`.
- The 2026-08-31 book disagreement that ran six hours and never resolved (finding 4).
  Separate investigation.
- `FillInfo.fill_price` as a durable column plus migration. Item B may strengthen the
  case for it; it does not build it.
- `/status` naming a residual order — a separate named follow-up from P5.

## Deliverable

**Item A**: the design questions answered in writing, then the change and the tests
above. Small enough that a single pass covering both is fine — but the A1 decision
(merge vs distinguish) should be stated and justified before the diff, not inferred
from it afterwards.

**Item B**: a written report only. The mechanism established from source, the list of
everything derived from `steps`, a recommendation among B3's three options, and a
plain statement of what it means for `walk_source`'s 84% figure. No code.

Both are `src/argus/tools/` and possibly `execution/loop.py` (item B, only after
sign-off). No config key, no migration, no runtime behaviour change. If the work
turns out to need any of those three, stop and say so.

A short decision-record note in `docs/decisions/` is owed only if item B changes a
field's meaning; item A on its own is a tool fix and belongs in the 2026-08-31 pass
record's finding 5 as a resolution line rather than in a new document.

Confirm with Richard whether to work on a branch or on `main`, and whether to commit.
Do not push unless explicitly told to.
