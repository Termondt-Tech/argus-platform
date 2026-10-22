# `/status` names what Argus is holding but not managing

**2026-09-02. BUILT. `src/` only, no config key, no migration, no new audit
category — the image deploys alone.** Designed first and signed off before any code:
[the design](../designs/2026-09-02-status-unmanaged-positions.md), approved as
written, all six §9 decisions with no amendments. Task 3 of the `/status` go-live
prompt.

**Closes two open decisions as one** — "Should `/status` name an UNWATCHABLE
position?" and "Should `/status` name a residual order?" — which is what the first of
them asks for: *one decision about what the page owes for held-but-unmanaged
positions generally, not a third one-off row.*

---

## What was wrong

Two states in which Argus holds something nothing is driving. Each paged Tier 1 once
per episode and then appeared **nowhere** on the page.

**An UNWATCHABLE position** (`OpenSpreadScan.unattributed` / `.malformed`). The open
decision described it as "no virtual stop is evaluated for it". That understates it,
and the design pass is where the rest was found: `check_once` iterates
`self._scan.spreads`, and such a position is by construction not in that tuple, so it
gets

| a watched spread | an unwatchable one |
|---|---|
| `evaluate_triggers` every tick | never called |
| credit + underlying stops | not evaluated |
| **`FLATTEN_BEFORE_CLOSE` at 15:30** | **never fires** |
| a live mark | none — nothing prices it |
| a row on `/status` | none, until now |
| a concurrency slot | **yes**, via broker legs ÷ 2 |

The last row is the sharp one. `RiskSnapshot.open_spread_equivalent` counts the
broker's LEGS, so the system correctly degrades toward not trading — and the page
showed the consumed slot with nothing anywhere saying where it went. On 0DTE, "no
flatten" means it rides to settlement.

**A residual order.** P5 (2026-08-30) built `ResidualOrderRecheck` with an
`outstanding()` accessor whose docstring already said "for the `/status` row that is
a separate change", and deliberately left the rendering out of that pass.

Both are the third and fourth instances of one pattern this page has been fixed for
twice — `daily_loss.breach` rendered TRIPPED off a log row, and a spent exit attempt
rendering as an ordinary open position. **A page that reads calm for a state that
needs a human.**

## What was built

One section, `unmanaged`, with **two rows**, in the existing positions block:

```
Open positions        legs / spreads / +working
Unwatched positions   ← NEW
Exit triggers         impaired
Exit attempts         spent
Residual orders       ← NEW
```

**The decision is one; the rendering is two, and that is not a hedge.** An operator at
15:00 on expiry day is choosing between two entirely different actions — *close this
by hand now* versus *decide whether to cancel an order P5 may already be handling* —
and merging the lists would force them to re-derive that split from each entry's shape
at the moment they have least attention. Same error `EntriesEndedToday` was written to
avoid one row over: a merged `monitor_only` flag would answer "are entries off?" and
lose "what clears it".

**Named `unmanaged`, not `degraded_positions`.** `_impaired_cell` already renders the
word "degraded" as the *milder* of its two severities, so a section two rows away using
it for something strictly worse than `BLIND` would be actively misleading. `unmanaged`
is also already this codebase's word for it (`watchdog.unmanaged_order_max_s`,
`unmanaged_order_cancel`).

**Ordering is narrative and severity agreeing.** The block is one escalating story
about the same held spreads — *what is held → is it watched at all → can its triggers
be evaluated → can it be exited* — and "on the watch list?" is the step before
"evaluable?". Severity says the same: an unmanaged position is strictly worse than a
BLIND one, which is at least still scanned, marked and flattened. Residual orders go
last because they are the only row about an ORDER, and the calmest.

### Severity

- **Unwatched positions: red, with one amber exception.** `settling` — a
  malformed-only condition inside `watchdog.book_settle_s` — is the ordinary mid-fill
  window, where the two legs of one multi-leg order book ~0.25s apart and a spread
  legitimately has one applied leg. `_report_unwatchable` suppresses its own page for
  exactly that, and the row reads it from the same fields rather than recomputing the
  rule. Colouring a routinely-benign condition red is how a red badge stops meaning
  anything (2026-08-05). **An `unattributed` symbol is never settling, at any age** —
  there is no pending second leg that would resolve it, so an allowance only delays
  the answer; `_report_unwatchable`'s asymmetry, verbatim.
- **Residual orders: amber, escalating to red on a read gap.** `last_sighting` of
  `absent` / `unreadable` / `ambiguous` is the state P5 treats as no verdict in either
  direction — the broker will neither confirm the order dead nor licence a cancel — so
  nothing is progressing, which is a different and less tractable problem than a cancel
  that failed. The row says that sentence, because a red row otherwise reads as "the
  cancel failed".

### The rows carry the sentences, not just the colours

The unwatched row says **"no stop, no mark, NO 15:30 flatten; needs a human"**. That
is the part that is not obvious: a reader who knows the watchdog is running will
assume all three, because for every other position on the page they hold. A colour
does not survive a phone screenshot pasted into Slack.

The residual row says the MODE once, on its summary line — `OBSERVER MODE
(would_cancel): nothing is being cancelled` — because the mode is a property of the
recheck, not of a residual, and repeating it per row would read as if it could differ
between them. **More useful while `unmanaged_order_cancel = false`, not less**: the
flip is a config change with no deploy, so the page is the only place an operator can
confirm which mode is live. A CLOSING residual additionally says *"never
auto-cancelled"* in either mode — `HaltCheckFn`'s asymmetry, because cancelling a
close strands a position.

## The seams

Two new live reads, both pure in-memory, both published from `run_session` inside the
existing `trading is not None` guard at the same instant as the other five:

```
PositionWatchdog.unwatchable_positions() -> tuple[UnwatchablePosition, ...]   NEW
ResidualOrderRecheck.residual_rows()     -> tuple[OutstandingResidual, ...]   NEW
```

**Read [the seam-drop record](2026-09-02-a-status-seam-drops-what-its-source-gained.md)
first — this is why it was fixed before this was designed.** The converters
(`_to_unmanaged_positions`, `_to_residual_rows`) are module-level in `session.py` and
join `_SEAMS`, so the structural sweep asserts each names every field of its source.
**The sweep's completeness test did its job on the commit that added them**: it failed
until both were written into the map, which is the whole reason it exists.

Three choices worth carrying:

**A new watchdog accessor, not a read of `_scan`.** `watching` already exposes
`self._scan.spreads`, so a caller could read `_scan.unattributed` directly. Rejected:
the row needs the episode's AGE and its `settling` verdict, both of which live in
`_report_unwatchable`'s private state, and re-deriving the settle allowance in the
status seam would be a second implementation of a rule that already exists — exactly
the "reconstructing state rather than reading it live" failure `ImpairedPosition`'s
docstring is about.

**`TradingSide.residuals`, not a watchdog passthrough.** `PositionWatchdog` holds
`self._residuals`, so the delegation was one line. Rejected because
`residual_orders.py`'s own docstring states the boundary: *the policy lives here rather
than in `watchdog.py`, which is a position-monitoring module whose docstring earns its
keep on that framing.* A read-only passthrough is the kind of small violation that
makes the next one easy.

**The one correction to the approved design.** §3.2 proposed
`_to_residual_rows(rr.outstanding(), cancel_enabled=rr.cancel_enabled)` plus a
`cancel_enabled` property. **That cannot work**: `ResidualOrder.registered_at` is on
the registry's own monotonic clock, so `working_for_s` cannot be computed anywhere
else — a seam differencing it against any other clock produces a number that looks
right and is not — and doing arithmetic in the converter would break the
field-for-field property the sweep asserts. Built instead as `residual_rows()`,
returning a FROZEN, clock-resolved, mode-stamped `OutstandingResidual`. That subsumes
the `cancel_enabled` property rather than adding to it (less `execution/` surface, not
more), and it stops a renderer holding a handle into mutable registry state that
`step()` writes — the same relationship `SpentExitAttempt` has to `_SpentAttempt`.
`outstanding()` is unchanged.

## The three-state contract, and the one place it can mislead

`None` = nothing publishing (shadow, or no session); `()` = live and everything
explained; non-empty = act. Both sources cleared in `session_finished` with the rest —
the watchdog and the registry are BOTH rebuilt per trading day, and the registry is
explicitly never rehydrated, so a source kept past teardown would show orders
belonging to a process state that no longer exists.

**On a `HaltGrade.ALL` parked process there is no session, so both rows read `None`.**
That is correct and it is *not* the reassuring reading — it is the same `None` a shadow
process shows. The HALTED row directly above says the process will not trade, so the
page as a whole is unambiguous. Recorded because it is the one place a reader could
take `None` for calm.

## Truncation

Nothing here goes near the audit tail's 240-char cut — that is what live rows exist to
sidestep. `problem` renders **verbatim** (it is `store.py`'s own sentence, under 70
characters in every shape, and it is the operator's whole diagnosis). Bounds are by
COUNT only: `MAX_UNMANAGED_ROWS = 8` per category and `MAX_UNMANAGED_SYMBOLS = 4`
inside a row, both module constants in the category `_MIN_UPTIME_FOR_RATE_S` and
`recent_limit` are already in.

`malformed`'s per-symbol dicts (`outstanding`, `divergent`) are flattened to
`(symbol, text)` pairs **in `watchdog.py`**, because that is the only module that knows
those shapes — pushing the branching downstream would make the status seam a second
reader of `store.py`'s internals. Past that point every field is a plain type and the
seam interprets nothing. The flattener is tolerant by construction: an unrecognized
shape yields no detail rather than raising, because a `/status` read must not be able
to fail on a record that is by definition already malformed.

## No try/except on the accessors, and why

The prompt asked for `read_rss_bytes`'s discipline — return None on any failure.
**Deliberately not done, and the reason is pinned instead of asserted.**

`read_rss_bytes` swallows because it does FILE I/O against a path that may not exist.
`unwatchable_positions()` and `residual_rows()` do dict, tuple and clock reads of
in-process state, exactly like `impaired_positions()` and `spent_exit_attempts()`,
neither of which is wrapped. A swallow here would render a genuine bug in the accessor
as **"nothing unmanaged"** — the reassuring-when-wrong failure this whole section
exists to remove — and would be inconsistent with the two rows it sits beside.

What holds the claim instead: an AST test asserting `unwatchable_positions` contains
no `await` and reaches none of `self._audit` / `_spreads` / `_executor` / `_notify` /
`_residuals`, i.e. that it cannot GROW an I/O path; plus a behavioural test that it
answers on a watchdog that has never scanned. If a swallow is ever wanted, it belongs
in `StatusService.assemble` — one place, all seven sources — and it must log.

## What was deliberately NOT changed

- **The scan, the recheck, and every halt path.** Every value rendered already existed
  in memory. Nothing new is measured and nothing new is decided.
- **No new audit category.** `watchdog.unwatchable` (Tier 1),
  `watchdog.unwatchable_still` (Tier 2), `watchdog.watchable_again` (Tier 1) and the
  `order.residual_*` family already carry the record; a Tier 2 "still unresolved"
  restatement would duplicate `watchdog.unwatchable_still` exactly.
- **An unwatchable position is not made watchable, and gets no flatten.** Tempting and
  wrong here: `FLATTEN_BEFORE_CLOSE` routes through `ExitExecutor.close`, which needs
  an `OpenSpread` — the thing that could not be built. It stays an open question.
- **Residuals still never rehydrate across a restart.** A `/status` row cannot invent
  durability the registry does not have.
- **Nothing pages.** Both conditions already page Tier 1 once per episode. This is the
  surface that was missing *between* pages.

## A known blind spot, stated rather than discovered later

2026-09-01 recorded `working_orders_unread: 66` — **one failed `get_live_orders()`
poll blinds BOTH P4's working-order count and P5's recheck.** That is not fixed here,
and the residual row goes quiet with it: no poll, no sighting update, and a registry
that keeps its rows but stops learning. The row still shows the orders and their ages;
what it cannot show is that the answer is stale.

## Deploy

`src/` only — `watchdog.py`, `residual_orders.py`, `session.py`, `status.py`. **No
config key**, so the image deploys alone and does not join the coupled
`watchdog.exit_walk_min_steps` deploy. Read-only: no order is sent, no rail changes.
24 tests in `tests/test_status_unmanaged.py` plus two more seams under the existing
sweep; **nine mutations verified, all killed** — dropping `settling` from the
converter, letting `unattributed` settle, rendering `settling` red, shrinking the
read-gap set, returning `()` where `None` is owed, stamping the wrong direction,
hardcoding the cancel mode, zeroing the episode age, and reordering the block.
