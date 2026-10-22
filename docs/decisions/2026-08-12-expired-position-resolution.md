# Expired-position ledger resolution (2026-08-12)

*A `positions` row that 0DTE expiry stranded is resolved at the next reconcile
instead of hard-halting the process.*

> Settled. See [`README.md`](README.md) for the index. Found by code review
> (`docs/post-close-outcome-processing.md` (private record, not published),
> Item 1), **not** by a live incident — nothing has routed a real order yet.

## The defect

A `positions` row leaves the table exactly two ways:

1. a fill event nets its signed quantity to zero (`store._apply_position`), or
2. `clear_ledger`, which fires only for the sandbox 24h wipe.

**0DTE expiry is neither.** The position simply ceases to exist at the broker,
with no closing trade and no fill event. Argus does receive `CurrentPosition`
frames, but `account.stream._ingest_position` writes the in-memory cache, never
the table — verified in source, as claimed. `positions` has no expiry column
(migration 0002: `symbol`, `underlying`, `signed_quantity`,
`average_open_price`, `opened_at`), so nothing noticed.

So the rows outlive the position they describe. The next reconcile — next
process or session start — compares them against a broker that holds nothing,
classifies `missing_at_broker`, and in sandbox/live that is the **process-wide
hard halt** (kill switch + stop, `source="reconciliation_mismatch"`). On the
strategy's most common *winning* outcome.

**Why sandbox would never have shown it.** `sandbox_reset_decision` clears the
whole ledger on a rolled-over trading day with nothing written today. Cert masks
this completely, every morning. It would have first fired on the live account.

## The ITM case — the source note's mental model was wrong, the conclusion holds

The review flagged as unverified that "an assigned ITM short produces a broker
transaction, so that case may resolve itself". Checked, and the framing does not
apply: **SPX/SPXW are cash-settled European-style index options.** There is no
assignment and no early exercise. An ITM contract is exercised automatically at
expiry and settled in cash against the closing print (SPXW is PM-settled), which
produces a cash *transaction* — not an order, not a leg fill, and nothing that
reaches `FillConsumer`, whose only input is `PlacedOrder` events.

So an ITM expiry strands the row exactly like a worthless one. That makes the
rule **more** necessary, not less, and no special case is needed — because the
rule keys on expiry, which is sign- and moneyness-agnostic. The economic side of
an ITM settlement (the cash debit) is Items 3/5 in the source document and is
untouched here; this change is about the ledger row only.

## The rule

Built into `reconcile.py`'s existing sequence, at the normal restart-time
reconcile. Not a new post-close phase: `reconcile_and_record` is already the ONE
entry point both refusal sites run, extending it adds no scheduling surface to a
safety-critical path, and the rule is evaluated per row against that row's own
expiry — so it behaves identically the same night or after a multi-day gap.

`expiry_verdicts` is the pure decision, and each half is load-bearing:

- **Broker-confirmed, never clock-assumed.** Only `missing_at_broker` rows are
  eligible — the broker holds *nothing* of that symbol. "It is past 16:00" is not
  evidence. `unexpected_at_broker` (the broker holds what Argus did not expect) is
  the opposite problem, and `quantity_mismatch` means some of it is still held, so
  whatever happened, it did not expire away. Both reach the halt untouched.
- **Strictly PAST expiry.** `expiry >= today` keeps the mismatch, so a row for
  today's expiry still halts even after the close. Within its own trading day the
  position can genuinely still be held, and the cost of being wrong in that
  direction is deleting Argus's record of a live position.
  **[SUPERSEDED 2026-08-25 — see the addendum.](#addendum-2026-08-25-the-evening-restart-and-the-legs-left-behind)**
  The reasoning holds and is unchanged for the intraday case; it does not survive
  SETTLEMENT, after which a 0DTE position definitively cannot still be held. A
  row expiring today now resolves once today's session has settled, and halts
  before that. Everything else in this list is untouched.
- **Unreadable expiry keeps the halt.** `occ.occ_expiry` returns `None` rather
  than raising for anything it cannot parse, and `None` resolves nothing.
- **It can only ever REMOVE rows from the discrepancy set, never add one.** `ok`
  is recomputed from what survives; order discrepancies ride through untouched.

The expiry comes from the OCC symbol because that is the only place it survives.
**No OCC parser existed** — the review assumed `signal/` had one, and it does
not: `OptionContract.expiration` comes straight off the SDK's
`option.expiration_date`, never from parsing. `src/argus/occ.py` is the new one,
deliberately a single function (`occ_expiry`) anchored on the fixed-width
15-character tail so padded and unpadded roots parse identically. Strike and
right are not exposed: they are already carried as typed fields everywhere they
are needed, and re-deriving them from a string would be a second, weaker source.

## Choices worth not relitigating

- **The delete goes through `store.py`.** `remove_positions` is a new scoped,
  by-row method rather than an adaptation of `clear_ledger` — that one wipes
  `orders`/`order_legs`/`positions` together for a completely different reason
  (a whole-account wipe), and widening it to take a filter would blur two events
  that must stay distinguishable. `positions` keeps exactly one writer, now
  asserted structurally (`test_order_store.py`, AST scan over `src/`).
- **The delete is quantity-guarded.** `DELETE ... WHERE symbol = ? AND
  signed_quantity = ?`, returning what was *actually* removed. The caller reasoned
  about a row it read through a different connection; a row that has moved since is
  not that row. A shortfall keeps its discrepancy and therefore keeps the halt —
  fail-closed by construction rather than by a check — and alerts
  (`expired_position_removal_incomplete`).
- **The order ledger is left alone.** `orders`/`order_legs` are the durable record
  of what was traded and the only place a spread's entry credit lives.
  `reconcile_orders` never treats a *recorded* order as a discrepancy, and
  `_open_spreads` already skips orders whose legs are no longer held. Nothing to
  gain, a P&L history to lose.
  **The DECISION stands; the second clause went stale.**
  `_open_spreads` skipped them by reading `positions`, and on 2026-08-17
  [overlapping verticals](2026-08-17-overlapping-verticals.md) moved holdings to
  per-idea `applied_quantity` — after which nothing removed an expired leg and
  this rule's delete left a permanent divergence behind it. Fixed in the
  [addendum](#addendum-2026-08-25-the-evening-restart-and-the-legs-left-behind)
  by teaching the scan about expiry, still without touching `order_legs`.
- **Its own audit category, distinct from the wipe.** `reconcile.expired_positions`
  (Tier 1, awaited) carries per-row verdicts — symbol, expected quantity, expiry,
  resolved, reason. `reconcile.sandbox_reset` is a different event: environment-
  gated, day-rollover-justified, and it clears everything. This one is row-scoped,
  environment-independent (expiry is a fact about the instrument, not about which
  endpoint is answering), and justified per row. A test asserts they are
  distinguishable rather than leaving it to inspection.
- **Recorded BEFORE the delete**, mirroring `_clear_ledger_if_reset` and for the
  same reason: a process that dies between the two leaves an explanation with the
  ledger still intact — the recoverable half.
- **Silent when there is nothing to say.** No missing rows at all (every clean
  reconcile) writes nothing. Missing rows that expiry does *not* explain get Tier 2
  `reconcile.expiry_check` carrying why each was refused — useful next to a halt,
  not worth a crash-critical write. Only an actual resolution is Tier 1.
- **Skipped without an order store**, exactly like the reset check. Shadow submits
  nothing, so nothing ever writes `positions` there.

## Checked and found not to be a problem

**Incomplete — see the
[2026-08-25 addendum](#addendum-2026-08-25-the-evening-restart-and-the-legs-left-behind).**
This checked `/status`'s `open_position_count` and stopped there; it never traced
the WATCHDOG's own spread reconstruction, which had a stake in the row this rule
deletes and reported a permanent false `watchdog.unwatchable` because of it.

`/status` does **not** surface `positions`. The source document wondered whether a
stale row would show as a phantom open position between the close and the next
reconcile; it would not. `StatusSnapshot.open_position_count` comes from
`RiskSnapshot`, which reads `AccountSnapshot.open_position_count` — the broker's
own account cache — and reads `None` when no session is running. The only DB
queries `/status` issues are against `audit_log`. No change was made there.

## Testing

`tests/test_expired_positions.py`. The load-bearing half is the negative
direction: today's expiry still halts, a future expiry still halts, an unreadable
symbol still halts, and the other two discrepancy kinds are not even eligible —
each on the pure rule and again end-to-end through `reconcile_and_record`. The
mixed case (two stale legs plus one genuinely-missing current-expiry row) proves
the rule is per row and not per run: the stale pair clears, the real mismatch
survives, the alert still fires. Two tests run on real SQLite connections for the
same reason `test_sandbox_reset.py` keeps one — the delete runs on the writer
connection while the expected set is read through a separate reader, and a reader
pinned to a pre-delete snapshot would refuse the day with the ledger already
empty, which is invisible to fakes.

---

# Addendum, 2026-08-25: the evening restart, and the legs left behind

*Two gaps in the analysis above, from one event. Neither is a regression; both
are things this document did not consider.*

The `v0.3.8` evening deploy restarted the process at **21:02 ET on 2026-08-24**, a
day that held two 0DTE spreads to expiry — a call spread and a put spread, both
settled worthless at 16:00, zero closing fills, the normal winning outcome. The
restart hard-halted on **4 `missing_at_broker` discrepancies**, all four of that
day's legs, and stayed halted until the ET date rolled over; it resolved cleanly
at 08:25 the next morning with no intervention. That is **Task 1** below.

The next morning's session then paged `watchdog.unwatchable` and repeated
`watchdog.unwatchable_still` every ~15s for both of the previous day's settled,
zero-risk positions. That is **Task 2**, and it is the one this document's own
"Checked and found not to be a problem" section missed.

Three of the five most recent trading days (08-24, 08-21, 08-20) held at least
one position to expiry, so an evening release on any of them would have hit the
first. Neither cost a trading day; both were worked around by waiting.

## Task 1 — a same-day expiry resolves once the session has SETTLED

**What the original rule said.** *"Strictly PAST expiry. `expiry >= today` keeps
the mismatch, so a row for today's expiry still halts even after the close.
Within its own trading day the position can genuinely still be held."* Sound for
an intraday restart, and the reasoning is preserved verbatim below — but it was
written for the intraday case and applied to the whole calendar day, evening
included.

**Why the concern stops applying at settlement.** SPXW is PM-settled, cash-settled
and European (§"The ITM case" above establishes this for the assignment
question; it does the same work here). After settlement a 0DTE position
**definitively cannot still be held** — there is no state of the world in which
the row describes something live. The thing the strictly-past rule protects
against has ceased to exist, so the rule is no longer conservative, only wrong.

**The new rule**, which extends eligibility without loosening any floor:

> A `missing_at_broker` row resolves when its OWN OCC expiry is strictly before
> today, **OR** is today AND today's session has already settled.

- **Broker-confirmed, never clock-assumed — untouched.** Only `missing_at_broker`
  rows are eligible, exactly as before. The clock decides whether the broker's
  silence is CONCLUSIVE yet; it never substitutes for the broker. A
  `quantity_mismatch` at 21:00 is still a mismatch.
- **Unreadable expiry keeps the halt — untouched.**
- **Can only ever REMOVE rows — untouched.**
- **A future expiry never resolves.** Settlement of TODAY's session says nothing
  about a later one. That half of the comparison is still absolute.

**Where the boundary comes from.** 16:15 ET — the close plus 15 minutes — the
number and the reasoning already in this codebase at `tools/mark.py`'s
`_EARLIEST_FEED_MARK_ET` (*"a quote before this is not a settlement price"*).
Reused rather than reinvented.

**Half-days are handled, not deferred.** The task filing anticipated that Argus
might have no early-close calendar and asked for the gap to be flagged rather
than hardcoded around. It has one: `MarketCalendar` already reads `[market]
half_days` / `half_day_close` from config (populated through 2027) and exposes
`close_time(d)`. So the margin is applied to **that day's own close**, not to a
literal 16:00 — `MarketCalendar.settlement_mark(d)` is 16:15 on a normal session
and 13:15 on a half-day, and `is_settled(d, now_et)` compares against it. The
one residual is the same one the calendar has always had and which this change
does not widen: those lists are **operator-maintained**, and the 2027 entries
carry their own RE-VERIFY note. A missing half-day entry makes this rule refuse
for three extra hours — safe, and the same direction the calendar already fails.

**The signature change, and why.** `expiry_verdicts(discrepancies, today)` gained
a **required keyword-only `session_settled: bool`**, and `reconcile_and_record`
gained the same parameter to thread it. Required-with-no-default follows
`reconcile_orders`' `unmappable` and for the same reason: both directions of a
default are a way to get this wrong without knowing the argument exists — `True`
would resolve a live 0DTE row at 11:00, `False` would silently restore the halt
this exists to end.

A bool rather than a `datetime` because the rule asks exactly one question, and
the thing that ANSWERS it needs the calendar, which `reconcile.py` deliberately
does not import (it stays importable by anything; see its module docstring on the
`Any`-typed ports). All three call sites build it identically:

| Call site | How |
|---|---|
| `session._reconcile_or_abort` | `session_settled(config, today, et_now())` |
| `session.startup_reconcile` | `session_settled(config, today, et_now())` |
| `app.Application._reconcile` (injected reconciler) | `session_settled(config, now.date(), now)` — ONE reading of its own `_now_et` seam, feeding both |

`session.session_settled` is the ONE place the input is built —
`MarketCalendar.from_config(config.market).is_settled(today, now_et)` — and the
CLOCK is the caller's, deliberately: `session.py` passes `clock.et_now()`, the app
passes its own injectable `_now_et` seam so a test that moves the clock past the
close moves the trading date and the verdict together. Never host-local time, and
never two readings that could straddle midnight and judge one day's expiry against
another day's close.

`is_settled` compares against an absolute instant rather than a time-of-day, so
"is today settled?" asked at 00:01 tomorrow answers `True` for free.

**Reason strings.** A same-day resolution gets its own — `SETTLED_AT_BROKER`
(`expired_today_and_session_settled`) — rather than reusing `EXPIRED_AT_BROKER`,
and the refusal splits into `KEPT_NOT_YET_EXPIRED` (later than today) and
`KEPT_SESSION_NOT_SETTLED` (today, too early). The audit payload also carries
`session_settled`, because it is the input a same-day verdict turned on and it is
not recoverable from the record afterwards: `ts` is UTC and the mark moves on a
half-day.

**Live is unaffected either way.** This only ever changes whether a halt is
refused or resolved automatically; `mismatch_halts_process` and the halt
behaviour around it are untouched.

## Task 2 — expiry-resolved positions left `order_legs` behind

**The mechanism.** `execution/store.py`'s `_open_spreads` reconstructs "what is
open" purely from `orders`/`order_legs` attribution — a trade idea's opening
applied quantities net of its closing ones — with **no notion of expiry**. Both
of the ways a leg could stop being outstanding were ORDERS, and **0DTE expiry
produces no order.** So a spread held to expiry stayed "outstanding" by that
arithmetic forever, while the `positions` rows it is checked against were removed
by the rule at the top of this document.

The result is a permanent `attributed != held` divergence for those symbols —
`_pair_spreads`'s invariant firing exactly as designed, on the one cause of
disagreement that is not one. Reported as `malformed` (*"ledger holdings disagree
with the positions table"*), which trips a Tier 1 `watchdog.unwatchable` page and
then repeats as Tier 2 `watchdog.unwatchable_still` every `refresh_interval_s`
(15s) indefinitely.

**Confirmed harmless to trading, and a real observability defect.**
`PositionWatchdog.open_spread_count` counts `self._scan.spreads` only, never
`malformed`, so this never blocked an entry or inflated the concurrency count.
But the page reads "UNWATCHABLE POSITION" for something that is not a position at
all, and the state is invisible on `/status` — the gap
[A2b](2026-08-21-a-spent-exit-attempt-is-visible-on-status.md) named and
deliberately left unbuilt.

**Why this document's "Checked and found not to be a problem" missed it.** That
section verified `/status`'s `open_position_count` and stopped there. It never
traced the WATCHDOG's own spread reconstruction, which reads a different table
through a different path and had a stake in the row this rule deletes. The
sentence *"`_open_spreads` already skips orders whose legs are no longer held"*
in "Choices worth not relitigating" is the one that went stale: it was true of
the symbol-keyed reconstruction that existed on 2026-08-12, and false from
2026-08-17, when
[overlapping verticals](2026-08-17-overlapping-verticals.md) moved attribution to
`applied_quantity` per trade idea and stopped reading `positions` for holdings at
all. Same shape as C2's expired comment — a claim about a collaborator that was
accurate when written and silently went false when the collaborator changed.

**Why it had never fired before.** `last_clean_reconciliation` reads only the
SINGLE LATEST `reconcile.result` row and requires `ok`. An evening halt (Task 1's
scenario) makes that row a failure, which correctly blocks
`sandbox_reset_decision` on the next restart — and the sandbox reset, unlike this
narrow path, clears `order_legs` AND `positions` together, masking the defect
completely. Every previous restart had either a clean marker still standing or no
intervening halt. **It takes both halves of this addendum to reach: an evening
halt, then an expiry-only resolution with no reset.**

**The fix: the scan becomes expiry-aware; `order_legs` is not touched.**
`_leg_is_expired(symbol, today)` — the same predicate, the same OCC source, the
same direction as the rule above — and a leg it answers `True` for is not
counted as outstanding. A spread whose every remaining leg has expired therefore
takes the existing fully-closed path: absent from the scan, not `malformed`, not
`unattributed`, claimed by nothing. Identical treatment to a cleanly closed
spread, which is what it is.

Scrubbing or marking `order_legs` at resolution time was rejected for the reason
already recorded above — *"nothing to gain, a P&L history to lose"* — and it
would have reintroduced, in narrower form, the hazard this document was written
to avoid.

- **The divergence check is NOT weakened.** This narrows what "outstanding" means
  for an EXPIRED leg only. An unexpired leg missing from `positions` — a real
  ghost, a real bug, a sandbox-reset-retained ledger — still diverges, is still
  `malformed`, and still pages. A test asserts exactly that, on the same ledger
  shape, differing only in the date it is read as of.
- **`_leg_is_expired` does NOT take Task 1's settlement carve-out.** Strictly
  before today, no clock. Reconciliation runs once at a known moment; this runs
  on the watchdog's refresh cadence all session, and the cost of being wrong here
  is a **held 0DTE spread silently leaving the watch list** with no stop being
  evaluated. Nothing is gained by resolving 15 minutes after the close either:
  the session is over, and the next reconcile clears `positions` before the
  watchdog scans again.
- **An unreadable symbol is not expired.** `occ_expiry` returns `None`, `None` is
  not a past date, and the leg stays outstanding and reaches the divergence
  check. Fail-closed, identically to the reconciliation rule.
- **`unattributed` is untouched**, and the one state this MOVES is documented: if
  a `positions` row for an already-expired contract ever survived a reconcile,
  its symbol is no longer claimed by any spread and reads as `unattributed`
  rather than `malformed`. That is the true statement — there is no spread left
  to be malformed — and it is not reachable in production, because reconciliation
  resolves those rows before any session's watchdog scans.
- **`execution/watchdog.py`'s paging logic is unchanged.** The reading in the task
  filing was right: the fix belongs in what the scan REPORTS, not in how the
  watchdog reacts. The only watchdog change is plumbing — a `trading_date` seam,
  matching the one `ExitExecutor` already has, which the session fills with its
  own date for the same stated reason.

**The signature change.** `OpenSpreadSource.open_spreads()` takes a **required
keyword-only `today: date`**, threaded to `_open_spreads(conn, today)` and
`_pair_spreads(rows, held, today)`. Required for the same reason as Task 1's:
the dangerous direction is silent. A date from the wrong clock — or defaulted
while the caller held a different one — drops a LIVE spread off the watch list.

**Checked, not assumed: no P&L-reporting collision.** `tools.mark` and
`tools.desk` reconstruct fills through `shadow.marker.read_executions`, reading
the AUDIT LOG, and neither touches `_open_spreads` (verified by search across
`src/`). Nothing else in `src/` calls it but the watchdog.

## Testing (addendum)

- `tests/test_expired_positions.py` — the rule's new dimension. Every existing
  boundary is now parametrised over both settlement states where the answer could
  differ (a past expiry resolves regardless; a future expiry and an unreadable
  symbol resolve under neither), plus the two new directions: today's expiry
  resolves settled, halts unsettled. The end-to-end evening-restart case
  reproduces 2026-08-24 through `reconcile_and_record`, and the real-SQLite
  today's-expiry test is parametrised over both settlement states so the
  separate-reader visibility is proven for the new branch too.
- The 16:15 boundary itself is tested where it lives, on the calendar:
  inclusive at the mark (house convention), one second early is early, the close
  itself is not settlement, 13:15 on a half-day, and correct across midnight.
  `session.session_settled` is asserted to go through `MarketCalendar` rather
  than a literal, driven by a fixed clock.
- `tests/test_open_spreads.py` — the reported shape (opening order, no closing
  fill, past expiry, `positions` already resolved) is absent from the scan
  entirely; the SAME ledger read on its own expiry day is still watched; a leg
  missing from `positions` for any OTHER reason still diverges; an unreadable
  symbol is never treated as expired; and both stores pair identically across the
  date boundary.
- `tests/test_watchdog.py` — the ledger is read as of the watchdog's own
  trading-date seam.
- Full suite green (1,472). The AST guards (`test_no_leaks.py`,
  `test_session_trading_side.py`, `test_order_build.py`, and `test_order_store.py`'s
  single-writer scan) are untouched and passing; neither change goes near the
  single-legging or `trades_for_real` boundaries.

## Still open after this

- **`/status` visibility of `scan.unattributed`/`malformed` for causes OTHER than
  a resolved expiry.** Task 2 makes the expiry case stop occurring rather than
  surfacing it, which is the better outcome for that one cause and no answer at
  all for the rest. Still the gap A2b recorded.
- **The half-day calendar is operator-maintained.** Not new, not widened by this,
  and the failure direction is refusal rather than a false resolution — but this
  rule is now a consumer of it.
