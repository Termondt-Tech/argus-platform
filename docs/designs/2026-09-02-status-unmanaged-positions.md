# Design — `/status` names what Argus is holding but not managing

**Status: SHIPPED 2026-09-02. Approved as written and built the same day.**
The normative record is
[`docs/decisions/2026-09-02-status-names-what-is-unmanaged.md`](../decisions/2026-09-02-status-names-what-is-unmanaged.md),
which carries **one implementation-level correction to this document**: §3.2's
`_to_residual_rows(rr.outstanding(), cancel_enabled=rr.cancel_enabled)` cannot work,
because `ResidualOrder.registered_at` is on the registry's own monotonic clock and
`working_for_s` therefore cannot be computed outside it — and doing the arithmetic in
the seam would break the field-for-field property the sweep asserts. Built as
`ResidualOrderRecheck.residual_rows()` returning a frozen, clock-resolved,
mode-stamped `OutstandingResidual` instead, which subsumes §3.2's option (a) rather
than adding to it. This file stays as the record of what was *rejected* and why.

Task prompt: [`docs/tasks/argus_status_version_and_blackout_expiry_implementation_prompt.md`](../tasks/argus_status_version_and_blackout_expiry_implementation_prompt.md)
(Task 3). Tasks 0, 1 and 2 of that prompt are built and are independent of this.

---

**Status when written: PROPOSED 2026-09-02, awaiting sign-off.** Approved as written
— all six §9 decisions accepted with no amendments.

Closes two open decisions in [`docs/decisions/README.md`](../decisions/README.md) —
"Should `/status` name an UNWATCHABLE position?" and "Should `/status` name a
residual order?" — as **one** decision, which is what the first of them asks for.

Working arrangement, as for the last three designs: **work on `main`, do not
commit, do not push.** Richard reviews the working tree.

---

## 0. What this changes, in one paragraph

`/status` reports calm for two states in which Argus is holding something it is not
managing. An **UNWATCHABLE position** (`OpenSpreadScan.unattributed` / `.malformed`)
is held with *no virtual stop evaluated, no mark taken and no 15:30 flatten*, because
`check_once` iterates `self._scan.spreads` and an unexplained position is by
definition not in it. A **residual order** (P5's `ResidualOrderRecheck`) is an order
left working at the broker that no walk is driving. Each pages Tier 1 once per
episode and then appears nowhere on the page — the third and fourth instances of one
pattern this page has already been fixed for twice (`daily_loss.breach` read off a
log row; a spent exit rendering as an ordinary open position). This adds **one
section, `unmanaged`, with two rows**, both live reads off the objects that already
hold the state, no new config key and no new audit category.

---

## 1. Recommendation in one paragraph

Add `UnmanagedPosition` and `ResidualOrderRow` to `status.py` (defined there, built by
`session.py`, exactly as `PositionEvaluability` / `UnclosablePosition` /
`EntriesEndedToday` already are), fed by a new pure `PositionWatchdog.unwatchable_positions()`
and by the existing `ResidualOrderRecheck.outstanding()` reached through a new
`TradingSide.residuals` field. Render them as **two adjacent rows in the existing
positions block**, ordered `Open positions → Unwatched positions → Exit triggers →
Exit attempts → Residual orders`, because that block already reads as one escalating
story about the same held spreads and "is anything watching it at all?" is the step
BEFORE "can its triggers be evaluated?". Keep the `None` / `()` three-state contract
verbatim. Surface P5's `would_cancel` mode once, on the residual row's header, read
off the recheck object rather than re-read from config. The section is named
`unmanaged`, not `degraded_positions`, for one reason given in §4.1.

---

## 2. What is actually true in source

Read before writing, and each of these is load-bearing below.

### 2.1 An unwatchable position gets nothing, not merely "no stop"

`PositionWatchdog.check_once` (`watchdog.py:2289`):

```python
for spread in self._scan.spreads:
```

`OpenSpreadScan` (`store.py:265`) carries `spreads`, `unattributed` and `malformed`
as three separate tuples. Anything in the second or third is **not in the first**, so
for such a position:

| What a watched spread gets | An unwatchable one |
|---|---|
| `evaluate_triggers` every tick | never called |
| `credit_multiple` / `underlying_near_short_strike` stops | not evaluated |
| `FLATTEN_BEFORE_CLOSE` at 15:30 | **never fires** |
| a live mark (`cost_to_close`) | none — nothing prices it |
| a row in "Exit triggers" / "Exit attempts" | in neither |
| a slot against `max_concurrent_positions` | **yes** — via broker legs ÷ 2 |

The last row is the sharp one. `RiskSnapshot.open_spread_equivalent` counts the
broker's LEGS, so an unwatchable position **does** consume a concurrency slot — the
system correctly degrades toward not trading — and the page shows the consumed slot
with nothing anywhere saying why. On 0DTE, "no flatten" means it rides to
settlement.

This is strictly worse than the two states the page *does* name. A `blind` impaired
position is still scanned, still marked, still flattened; a spent exit attempt has
been evaluated on every tick since 2026-08-28 and carries its mark. An unwatchable
position has left the loop entirely.

### 2.2 The episode state the watchdog already keeps

`_report_unwatchable` (`watchdog.py:2818`) holds three fields for edge-triggering:
`_unwatchable_signature` (a `json.dumps` of the detail, sorted), `_unwatchable_since`
(monotonic, reset when the signature changes) and `_unwatchable_paged`. It applies a
`book_settle_s` allowance to a **malformed-only** condition — the two legs of one
multi-leg order book ~0.25s apart, so the watchdog aims straight at a window where a
spread legitimately has one applied leg — and **`unattributed` never waits**, because
there is no pending second leg that would resolve it.

Every one of those is reusable as-is. Nothing here needs new bookkeeping.

### 2.3 `malformed` entries are heterogeneous dicts

`store.py` appends six shapes, all carrying `problem` and varying in the rest:

| `problem` | also carries |
|---|---|
| `cannot determine which trade idea this order belongs to` | `broker_order_id`, `external_identifier`, `candidate_id` |
| `closing fills with no opening order on record` | `candidate_id` only |
| `outstanding legs are not one short and one long` | detail + `outstanding: {symbol: qty}` |
| `opening order limit <x> is not a credit` | detail |
| `ledger holdings disagree with the positions table` | detail + `divergent: {symbol: {attributed, positions}}` |
| (plus the remaining `detail`-only cases) | — |

So the status type must tolerate `candidate_id`, `external_identifier` and
`broker_order_id` each being absent, and must not assume a symbol list exists.

### 2.4 The residual registry is already reachable and already public

`ResidualOrderRecheck.outstanding()` (`residual_orders.py:416`) exists and its
docstring says, verbatim, "For tests, and for the `/status` row that is a separate
change." `ResidualOrder` carries `broker_order_id`, `external_identifier`,
`minted_direction`, `candidate_id`, `walk_outcome`, `cancel_requested`,
`registered_at`, `attempts`, `next_attempt_at`, `last_sighting`, `last_status`.

`session._build_trading_side` builds the recheck as a **local** and hands it to three
consumers; `TradingSide` (`session.py:667`) does not carry it. `PositionWatchdog`
*does* hold `self._residuals` (line 1907), so a `watchdog.residual_orders()`
delegation is possible — see §4.2 for why this proposes not to.

### 2.5 What is NOT stale, and one thing that is

Two coordination notes in the task prompt name the kill-switch-persistence and
`_end_day` prompts as *pending* and ask this design to "leave a slot" for them.
**Both shipped 2026-09-01** and both already render: `killswitch_restored` rides in
the kill-switch badge, and `entries_ended` is its own row with its own dataclass. So
there is no slot to leave — the vocabulary they established is instead what this
follows (see §4.1, §4.7). Reported rather than silently worked around.

---

## 3. The design questions

Numbered as the prompt numbers them.

### 3.1 (Q1) One combined section, two rows — and it is called `unmanaged`

**Recommended: one section, two rows inside it, positions first.**

The open decision is explicit that this "wants one decision about what the page owes
for held-but-unmanaged positions generally, not a third one-off row," and that is
answered by making the *decision* one, not by collapsing the *rendering* into one
list. An operator on a phone at 15:00 on expiry day is deciding between two entirely
different actions:

- **an unwatchable position** → *there is money at risk with no stop and no flatten;
  I have to close this by hand, now.*
- **a residual order** → *there is an order working that nobody is driving; the
  question is whether to cancel it, and P5 may already be doing so.*

Merging them into one list would force the reader to re-derive that split from each
entry's shape, at the moment they have least attention. That is precisely the error
`EntriesEndedToday` was written to avoid one row over: a merged `monitor_only` flag
would have answered "are entries off?" and lost "what clears it", and the operator
already knows the first.

**On the name.** The prompt proposes `degraded_positions`. Recommend **`unmanaged`**
instead, for a concrete collision: `_impaired_cell` already renders the word
"degraded" as the *milder* of its two severities ("degraded" vs "BLIND"), so a
section called `degraded_positions` sitting two rows away and meaning something
strictly worse than `BLIND` would be actively misleading. `unmanaged` is also already
this codebase's word for exactly this — `watchdog.unmanaged_order_max_s`,
`watchdog.unmanaged_order_cancel` — and the prompt's own framing ("held-but-unmanaged
positions").

### 3.2 (Q2) The live-read seam

**Recommended:**

```
PositionWatchdog.unwatchable_positions() -> tuple[UnwatchablePosition, ...]   # NEW, pure
ResidualOrderRecheck.outstanding()       -> tuple[ResidualOrder, ...]         # exists
```

published from `run_session` inside the existing `if trading is not None:` guard, via
two new module-level converters beside the three added on 2026-09-02:

```python
def unwatchable(wd: PositionWatchdog = trading.watchdog):
    return _to_unmanaged_positions(wd.unwatchable_positions())

def residuals(rr: ResidualOrderRecheck = trading.residuals):
    return _to_residual_rows(rr.outstanding(), cancel_enabled=rr.cancel_enabled)
```

**Read Task 0 first, which is why the converters are module-level.** The
`spent_exits` closure silently dropped three of `SpentExitAttempt`'s eight fields for
five days because it was written inline with the five fields that existed at the
time, and `UnclosablePosition`'s `None` defaults swallowed the omission at every
layer. Task 0 extracted all three converters to module level, added a behavioural
test that drives a real watchdog through to rendered HTML, and added a structural
sweep asserting each converter names every field of its source type. **That sweep has
a completeness test** — `test_the_sweep_covers_every_converter_in_the_seam_block` —
which will FAIL the moment these two converters are added without a `_SEAMS` entry.
This design inherits that protection by construction rather than by anyone
remembering.

**Why a new watchdog accessor rather than reading the scan directly.** `watching`
already exposes `self._scan.spreads`, so a caller could read `wd._scan.unattributed`.
Rejected: the row needs the episode's AGE and its `settling` verdict, both of which
live in `_report_unwatchable`'s private state, and re-deriving the settle allowance
in the status seam would be a second implementation of a rule that already exists —
the exact "reconstructing state rather than reading it live" failure
`ImpairedPosition`'s docstring is about.

**Why `TradingSide.residuals` rather than `watchdog.residual_orders()`.** The
watchdog does hold `self._residuals`, so the delegation is one line. Rejected because
`residual_orders.py`'s own docstring states the boundary: *"The policy lives here
rather than in `watchdog.py`, which is a position-monitoring module whose docstring
earns its keep on that framing."* A read-only passthrough is a small violation, but it
is the kind that makes the next one easy. Adding `residuals: ResidualOrderRecheck` to
`TradingSide` costs one field on a dataclass whose docstring is already about "built
together or not at all" — which the recheck is.

**One `execution/` addition, and it needs sign-off.** `ResidualOrderRecheck._cancel_enabled`
is private; the row must say which mode P5 is in (§3.5). Two options:

- **(a) recommended** — a `cancel_enabled` read-only property on
  `ResidualOrderRecheck`. Three lines, no behaviour change, and it obeys "read the
  rail, never infer it": the object that decides is the object asked.
- **(b)** — read `config.watchdog.unmanaged_order_cancel` in `session.py`. No
  `execution/` change at all. It cannot drift today (config is frozen and read once),
  but it is a second reader of the same setting, which is how `breaker_tripped` got
  its own bug.

The task's constraints say Task 3 "does not change scan logic, the recheck, or any
halt behaviour" — (a) changes none of those. Recommending (a); (b) is available if
"nothing in `execution/`" is meant literally.

### 3.3 (Q3) The `None` / `()` three-state contract, verbatim

Unchanged from the existing rows, and both sources published at the same instant
inside the same `trading is not None` guard:

| | `unmanaged.positions` | `unmanaged.orders` |
|---|---|---|
| shadow, or no session | `None` — "not applicable, nothing is watching" | `None` |
| live watchdog, nothing wrong | `()` — "the scan explains everything" | `()` — "no order was left working" |
| something wrong | non-empty | non-empty |

Both cleared in `SessionStatus.session_finished`, with the rest of the live handles
and for the same reason `spent_exit_source` is: the watchdog and the recheck are
rebuilt per trading day, so a source kept past teardown is a handle into last
session's object and turns the row into a process-lifetime tally.

**The one thing worth stating out loud**: on a `HaltGrade.ALL` parked process there
is no session, so both rows read `None` — "no watchdog" — and that is correct but is
*not* the reassuring reading. It is the same `None` a shadow process shows. The
HALTED row directly above already says the process will not trade, so the page as a
whole is unambiguous; no extra wording is proposed. Flagged because it is the one
place a reader could take `None` for calm.

### 3.4 (Q4) Severity and ordering

**Recommended order in the positions block:**

```
Open positions        (legs / spreads / +working)
Unwatched positions   ← NEW
Exit triggers         (impaired)
Exit attempts         (spent)
Residual orders       ← NEW
```

The prompt asks about severity, and severity alone would put "Unwatched" first. The
stronger argument is that this block is already a **narrative** about one set of
spreads — *what is held → can we evaluate it → can we exit it* — and "is it on the
watch list at all?" is the step before evaluability. Severity and narrative agree
here, which is why this ordering is proposed rather than argued for.

Residual orders go LAST in the block because they are the only row about an ORDER
rather than a position, and because they are the calmest: P5 re-cancels an opening
residual automatically (once promoted), and a residual that resolves on the next poll
is the common case.

**Severity within the rows:**

- `Unwatched positions`: **red, always, no gradient** — same posture as
  `_spent_exit_cell`, for the same reason. There is no `degraded`-equivalent state
  here; every entry means a held position with no stop, no mark and no flatten. The
  ONE exception is `settling` (§3.2): a malformed-only condition inside
  `book_settle_s`, which is the ordinary mid-fill window, renders **amber** and says
  "settling" — mirroring `_impaired_cell`'s `blind`/`degraded` split, and for the
  identical reason (colouring a routinely-benign condition red is how a red badge
  stops meaning anything, 2026-08-05).
- `Residual orders`: **amber**, not red. An order left working is exposure, not a
  position, and P5 is (or will be) acting on the opening ones. It escalates to
  **red** on one condition: `last_sighting` is a read gap (`ABSENT` / `UNREADABLE` /
  `AMBIGUOUS`), which is the state P5 treats as "not a verdict in either direction" —
  the broker will neither confirm it dead nor license a cancel.

### 3.5 (Q5) The `would_cancel` / `cancel` mode is surfaced once, on the row

**Recommended: on the row's summary line, not per entry.** The mode is a property of
the recheck, not of a residual, and repeating it on every row would read as if it
could differ between them.

```
Residual orders   2 working — OBSERVER MODE (would_cancel): nothing is being cancelled
Residual orders   2 working — auto-cancel ON for opening orders
```

This is more useful while `unmanaged_order_cancel = false`, not less: the row and
`order.residual_would_cancel` telemetry are the only surfaces showing what the
recheck *would* have done, and the flip to `true` is a config change with no deploy —
so the page is the only place an operator can confirm which mode is actually live.

Per entry, the row shows: `candidate_id` (or `external_identifier` when there is no
candidate), `broker_order_id`, `minted_direction`, how long it has been working,
`attempts`, `last_status` and `last_sighting`. **`minted_direction`, never
`Order.direction`** — P5's rule, because the broker relabels leg actions, and it is
also what says whether the automatic re-cancel applies to this row at all (opening
yes, closing never).

### 3.6 (Q6) Truncation

Nothing here goes near the 240-char audit-payload cut: this is a live read of typed
fields, which is the whole reason `ImpairedPosition` exists. Two specific bounds are
proposed anyway, both by COUNT and never by characters:

- `problem` renders **verbatim**. The six strings in `store.py` are all under 70
  characters and each one is the operator's entire diagnosis.
- symbol lists (`outstanding`, `divergent`) render as at most **4 symbols, then
  `+N more`**. A partially-closed remainder has 1–3 symbols in practice; the cap
  exists so a pathological ledger cannot produce a page that has to be scrolled past.
- at most **8 unwatchable entries and 8 residual rows**, then `+N more`. Same reason.
  Both counts are module constants in `status.py`, the category `_MIN_UPTIME_FOR_RATE_S`
  and `recent_limit` are already in — a rendering bound, not a rail.

`divergent`'s counts (`attributed` vs `positions`) render inline per symbol, because
"the ledger says 2 and `positions` says 1" is the diagnosis, not a detail.

### 3.7 (Q7) `SessionPhase` and the HALTED row are untouched

Both new rows sit inside the positions block, below the phase, the HALTED row and the
Entries row. Nothing about their meaning changes:

- `HaltGrade.ENTRIES` and `EntriesEnded` both leave the watchdog running, so these
  rows are live and meaningful under both. That is a feature of where they sit, not
  something to encode.
- `HaltGrade.ALL` parks with no session, so both read `None` — see §3.3.

No new top-level badge, no change to the page title, no change to `_PHASE_CLASS`.

---

## 4. The change, file by file

| File | Change |
|---|---|
| `execution/watchdog.py` | `unwatchable_positions()` — a pure read of `self._scan.unattributed` / `.malformed` plus `_unwatchable_since` and the settle verdict. New `UnwatchablePosition` frozen dataclass beside `ImpairedPosition` / `SpentExitAttempt`. No behaviour change. |
| `execution/residual_orders.py` | `cancel_enabled` read-only property (§3.2, option (a)). Nothing else. |
| `session.py` | `TradingSide.residuals`; two module-level converters `_to_unmanaged_positions` / `_to_residual_rows`; two one-line closures in `run_session`; two new kwargs on `session_running`. |
| `status.py` | `UnmanagedPosition`, `ResidualOrderRow`; two `SessionStatus` sources with the three-state contract; two `StatusSnapshot` fields; the `unmanaged` JSON block; `_unwatchable_cell` / `_residual_cell`; two rows in `to_html`. |
| `docs/decisions/README.md` | Mark both open decisions resolved; add the decision record's row. |
| `CLAUDE.md` | A2b's "deliberately NOT built" note and P5's "named follow-up" note both become built. |

**No new config key. No new audit category.** `watchdog.unwatchable` (Tier 1),
`watchdog.unwatchable_still` (Tier 2), `watchdog.watchable_again` (Tier 1) and the
`order.residual_*` family already cover the record; a Tier 2 "still unresolved"
restatement would duplicate `watchdog.unwatchable_still` exactly.

### 4.1 Proposed shapes

```python
# status.py
@dataclass(frozen=True)
class UnmanagedPosition:
    kind: str                          # "unattributed" | "malformed"
    problem: str                       # the sentence store.py wrote
    symbols: tuple[str, ...] = ()      # OCC symbols involved, may be empty
    detail: tuple[tuple[str, str], ...] = ()   # (symbol, "ledger 2 / positions 1")
    candidate_id: str | None = None
    external_identifier: str | None = None
    broker_order_id: int | None = None
    unwatchable_for_s: float = 0.0     # the EPISODE's age, per _unwatchable_since
    settling: bool = False             # malformed-only, inside book_settle_s

@dataclass(frozen=True)
class ResidualOrderRow:
    broker_order_id: int
    external_identifier: str
    candidate_id: str | None
    minted_direction: str              # NEVER the broker's relabelled action
    walk_outcome: str
    cancel_requested: bool
    attempts: int
    working_for_s: float
    last_status: str | None
    last_sighting: str | None
    cancel_enabled: bool               # P5's mode; identical on every row
```

`cancel_enabled` sits on the row rather than on a separate snapshot field so the
three-state `None` contract covers it too: with no session there is no mode to
report, and a top-level boolean would have to invent `False`.

### 4.2 JSON

```json
"unmanaged": {
  "positions": [ { "kind": "malformed", "problem": "...", "symbols": ["..."],
                   "candidate_id": "...", "unwatchable_for_s": 812.4,
                   "settling": false } ],
  "orders":    [ { "broker_order_id": 123, "minted_direction": "open",
                   "working_for_s": 240.0, "attempts": 3,
                   "last_status": "Cancel Requested", "last_sighting": "working",
                   "cancel_enabled": false } ],
  "cancel_mode": "would_cancel"
}
```

`null` for either list when nothing is publishing, matching `watchdog.impaired_positions`.
`cancel_mode` is echoed at the section level for a script that only wants the mode;
it is derived from the rows, not stored twice.

---

## 5. What this deliberately does NOT do

- **It does not change the scan, the recheck, or any halt behaviour.** Every value
  rendered already exists in memory; nothing new is measured and nothing new is
  decided.
- **It does not make an unwatchable position watchable.** The row says a held
  position has no stop; making one is a different, larger question (`_open_spreads`
  cannot reassemble it — that is what `malformed` means).
- **It does not add a flatten for unwatchable positions.** Tempting, and wrong here:
  `FLATTEN_BEFORE_CLOSE` routes through `ExitExecutor.close`, which needs an
  `OpenSpread` — the thing that could not be built. Naming it as an open question
  instead.
- **It does not rehydrate residuals across a restart.** P5 is explicit that the
  registry is in-process and never rehydrated; a `/status` row cannot invent
  durability the registry does not have.
- **It does not page.** Both conditions already page Tier 1 once per episode. This is
  the surface that was missing between pages, not another page.

---

## 6. Tests

| Test | Why |
|---|---|
| an unattributed symbol appears on `/status`, in JSON and HTML | the headline |
| a malformed order appears, with its `problem` verbatim | the six shapes are the diagnosis |
| both clear when the scan explains everything again | mirrors `watchdog.watchable_again` |
| a malformed-only condition inside `book_settle_s` renders amber "settling", not red | the mid-fill window is routine |
| an `unattributed` entry is NEVER "settling", at any age | `unattributed` never waits (`_report_unwatchable`) |
| the episode age comes from `_unwatchable_since` and survives a signature-preserving refresh | "still not clearing after 20 minutes" must be true |
| a residual order appears with its id, direction, age, attempts and last status | the row's content |
| the row says `would_cancel` when `unmanaged_order_cancel = false`, and `cancel` when true | Q5; the flip is a config change with no deploy |
| a residual reporting a read-gap sighting renders red, not amber | absence is not a verdict |
| a CLOSING residual is shown, and is not described as awaiting an automatic cancel | P5's asymmetry, verbatim |
| shadow / no session renders `null`, never `[]` | the three-state contract |
| session end clears both | not a process-lifetime tally |
| **through the seam**: a real `PositionWatchdog` whose scan carries an unattributed symbol renders the row — no hand-built status row anywhere | Task 0's rule; the defect it exists for |
| the structural sweep covers both new converters | `_SEAMS` completeness test fails until they are added |
| `unwatchable_positions()` performs no I/O and does not raise on an empty/torn-down watchdog | §7 |

Mutations to verify the tests can fail: drop `settling` from the converter (the sweep
must fail); return `()` instead of `None` with no watchdog (the contract test must
fail); read `Order.direction` instead of `minted_direction` (the direction test must
fail).

---

## 7. "The accessor must not take down the scan or the page"

The prompt asks for `read_rss_bytes`'s discipline — return `None` on any failure.
**Recommend NOT wrapping these in try/except, and pinning the reason instead.**

`read_rss_bytes` swallows because it does FILE I/O against a path that may not exist.
`unwatchable_positions()` and `outstanding()` do neither: they are dict/tuple reads of
in-process state, exactly like `impaired_positions()` and `spent_exit_attempts()`,
neither of which is wrapped. Adding a swallow here would be inconsistent with the two
rows these sit beside, and — worse — would make a genuine bug in the converter render
as "nothing degraded", which is the reassuring-when-wrong failure this whole section
exists to remove.

What is proposed instead, matching the repo's habit of pinning invariants
structurally: an **AST test** asserting `unwatchable_positions` contains no `await`,
no call to `self._audit`, and no call to `self._spreads` — i.e. that it cannot become
an I/O path later. Plus a behavioural test that it answers on a watchdog whose scan
is empty and whose episode state is unset.

Open to being overruled — if the preference is a swallow, it belongs in
`StatusService.assemble` (one place, all five sources) rather than in each accessor,
and it should log rather than return quietly.

---

## 8. Deploy shape and risk

`src/` only. **No new config key**, so the image deploys alone — unlike the coupled
deploy `watchdog.exit_walk_min_steps` already forces for the next release. Read-only:
no order is sent, no rail changes, no scan logic moves. The realistic failure mode is
a rendering bug on a page nobody was reading for this state anyway; the realistic
benefit is that the *next* time a position goes unwatchable, `/status` says so instead
of the operator finding it in a Slack page they scrolled past.

Worth noting for the release note: 2026-09-01 recorded `working_orders_unread: 66` —
one failed `get_live_orders()` poll blinds BOTH P4's working count and P5's recheck.
That is not fixed here and this row will go quiet with it. It should be stated in the
decision record rather than discovered later.

---

## 9. On sign-off

Decisions needing an explicit yes/no:

1. **§3.1** — one section, two rows, positions before orders, named `unmanaged`
   rather than `degraded_positions`.
2. **§3.2** — the new `PositionWatchdog.unwatchable_positions()` accessor, and
   `TradingSide.residuals` rather than a watchdog passthrough.
3. **§3.2** — `ResidualOrderRecheck.cancel_enabled` (option (a), a 3-line property in
   `execution/`) vs reading the config key in `session.py` (option (b), nothing in
   `execution/`).
4. **§3.4** — the row ordering, and `settling` as the one amber state on an otherwise
   uniformly red row.
5. **§3.5** — the mode on the row's summary line, per-row fields as listed.
6. **§7** — no try/except; an AST test that the accessor cannot become an I/O path.

Everything else follows from those six.
