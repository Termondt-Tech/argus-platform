# The second v0.3.13 day (2026-09-01)

**Status: REVIEW PASS. Twelve findings, none fixed here.** Two are recorded as
PROVEN-first-time rather than as defects. Three passes: `/status` + the alert trail,
then `tools.desk`/`tools.mark`, then the audit chain. Each found something the one
before it could not see. **[Addendum](#addendum--2026-09-01-evening-v0314-deployed-and-the-persistence-rail-proved-itself-across-the-version-boundary)
added the same evening**: v0.3.14 was deployed at 17:02 EDT, its first boot restored
a four-day-stale kill-switch trip that both live-fire days had traded through
(persistence proven end to end, across the code-version boundary), and the same
`/status` pulls exposed a startup-contamination defect in the per-hour rate fields
finding 7 relies on.

**The headline from `desk`/`mark`: two of the day's five `credit_multiple` stops were
mechanical artifacts rather than market events**, one of them a floating-point
epsilon. **The headline from the chain: an exit that SUCCEEDED was recorded as a
failure** — the broker refused a reprice *because the original order had just filled*,
and the walk read the replacement's rejection as the spread's outcome. Both of the
first pass's open questions are now closed, and two standing open questions elsewhere
in `docs/decisions/` are closed with them.

The image is **v0.3.13 (Tejat)**, deployed 2026-08-30, running unbroken since
12:26 ET that day (`uptime_s` 186120.7 at the 16:08 snapshot). It carries none of
`main`'s unreleased work — **not** the near-tick exit ladder fix
(`watchdog.exit_walk_min_steps`), **not** kill-switch persistence + `POST /resume`,
**not** `_end_entries_today`. Every finding below is against the deployed image, and
three of them are already answered on `main`.

## What the day was

Busy, and rough on the cert side. From `session_counts`:

| | |
|---|---|
| Candidates / gate decisions | 2714 / 2714 |
| `entry.finished` | 11 |
| Positions opened | 8 |
| `watchdog.triggered` / `exit_started` / `exit_finished` | 8 / 8 / 8 |
| `account.refresh_failed` / `refresh_recovered` | 498 / 164 |
| `account.stream_disconnected` / `stream_reconnected` | 14 / 1 |
| `walk.disconnected` | 7 |
| `watchdog.check` | 36257 |

Eleven trade ideas reached a walk. Seven filled through the ordinary path, one
(`put-7600-7595`) filled through the **residual registry** after its walk had given
up, and three died `no_order_exists`. Eight positions, eight exits. Six exits filled
on the ordinary stop, two were flattened at 15:30 by crossing the market.

`_end_day` was never reached — `entry.guard_unreachable_attempt` is 1 and the burst
recovered — so the deployed image's `_end_day`-stops-the-watchdog defect had no
exposure today.

## PROVEN for the first time

**P5's residual-order recheck saved a trade idea.** At 13:44 the walk for
`put-7600-7595` could not read order 1430443 after three attempts (cert
`internal_server_error` on `/customers/me/permitted`, Request Timed Out) and finished
`UNRESOLVED`. `_finish` registered the abandoned order; the watchdog's next
`get_live_orders()` poll saw it **FILLED** and emitted `order.residual_filled` /
`residual_order_filled` with `minted_direction: "open"`. The position was then
adopted, watched, and flattened at 15:31. This is the whole P5 arc — register at
`_finish`, recheck on P4's poll, direction from the minted identifier and never from
the broker — running end to end on a real order for the first time.

Two things this proves and one it does not. It proves the registry catches the case
it was built for, and it proves `minted_direction` was read (a fill on an OPENING
order, correctly labelled). It does **not** prove the automatic re-cancel: the order
filled, so nothing was cancelled, and `watchdog.unmanaged_order_cancel` is still
`false` — the recheck remains in `would_cancel` mode.

**The 2026-08-26 entry-guard retry burst fired and recovered.** One
`entry.guard_unreachable_attempt`, no `entry_guard_unreachable_abandoned`, no
`_end_day`. Before v0.3.11 that single unanswered `has_conflicting_order` ended the
session. It cost nothing today.

**P2's `watchdog.unevaluable` page fired once, correctly.** At 14:21 `put-7595-7590`
went unevaluable with `unknown_reasons: ["credit_multiple", "underlying_near_short_strike"]`
and `seconds_to_flatten: 512.79`. The runway field is doing exactly the job it was
added for: "triggered, unpriceable" with 8½ minutes left reads very differently from
the same page with 25 minutes left.

## The day's economics, and two stops that were artifacts

`tools.desk` and `tools.mark --dry-run --from-feed`, run at 16:16 ET:

```
DAY
  realized (closed intraday)             -575.00
  unrealized (held, last mark)              0.00
  fees on filled orders                   -89.54
  total                                  -664.54

8 idea(s) actually filled (8 closed intraday): scored -575.00;
  the same positions held to settlement would have been 975.00
  -> exiting early cost 1,550.00
```

SPX settled at 7637.705. **All eight spreads would have expired full-credit** — both
call spreads (7665/7670, 7670/7675) settled out of the money, and every put spread
(7590 through 7630) settled above its short strike. Five of the eight were stopped
out on `credit_multiple`, one on `underlying_near_short_strike`, two were flattened.

**Read that $1,550 correctly, and do not use it to argue against the stop.** This
was one trending session that mean-reverted, which is the single regime in which a
stop-loss is maximally wrong; a day where the stop saved the book would produce an
equally lopsided number in the other direction. One day is not evidence about
`credit_multiple`, and CLAUDE.md's existing prohibition still binds for a second
reason given below.

**What IS unconditional is that two of the five credit stops were not market events.**

### The float-epsilon stop

`call-7670-7675`, from the 09:34 page verbatim:

```
"cost_to_close": 1.6000000000000005, "credit_stop": 1.6, "fired": ["credit_multiple"]
```

The trigger is a bare `>=` on two floats (`watchdog.py:577`), and `credit_stop` is
`float(entry_credit) * credit_multiple` (`watchdog.py:465-467`). The position was
stopped out because a mid computed from four quotes exceeded its threshold by
**4.4e-16** — eleven orders of magnitude below the 0.05 tick, and below any price
difference that can exist. Realized −160.00 on a spread that expired worthless.

This is the **2026-08-04 mark-tool defect on the live trigger path**: *"a credit is a
half-tick ($0.025) quantity, so 2dp broke ties on float noise."* That one was fixed
with `Decimal`; it was a display column. This one closes positions.

> **FIXED 2026-09-02, and the `Decimal` parallel does not carry over.** The sentence
> above points at `watchdog.py:465-467` as if the multiplication were where the
> precision goes; it is not. `float(Decimal("0.8")) * 2.0` is **exactly** the double
> nearest 1.6, so a `Decimal` comparison would have fired identically — the noise is
> upstream in `spread_mid`, over four float quotes, on a function the ENTRY path
> shares and which prices the exit order. The fix is a tolerance on the comparison
> (`PRICE_EPSILON`, one millionth of a point), not a type change on the quantity.
> **The test suite turned out to contain a live specimen**: its "triggered" fixture
> netted 1.7000000000000002 against a 1.7 stop, so 23 watchdog tests were passing on
> a 2.2e-16 breach.
> → [the credit stop's two wrong numbers](2026-09-02-the-credit-stop-basis-and-its-comparison.md)

### The stop that was tight because `entry_credit` is the limit, not the fill

`put-7630-7625`: the watchdog recorded `entry_credit: 0.45` (the walk's
`final_limit_price`) and stopped at `cost_to_close: 0.925` against
`credit_stop: 0.90`. `desk` reconstructs the credit actually collected as **0.50**
from the fill records. On that basis the stop is 1.00, and 0.925 never reaches it —
**the position is not stopped out at all.** The conclusion is robust to the
reconstruction: this walk repriced once (`steps: 1`), so even a blended 0.475 across
two rungs puts the stop at 0.95, still above 0.925.

Three of the eight positions show the same discrepancy and **all three go the same
way** — the fill collected more credit than the limit Argus recorded:

| Candidate | `entry_credit` (recorded) | `desk` (from fills) |
|---|---|---|
| `put-7630-7625` | 0.45 | 0.50 |
| `put-7615-7610` | 0.75 | 0.775 |
| `put-7595-7590` | 0.55 | 0.60 |

This is the open finding CLAUDE.md already carries from 2026-08-20 —
*"`OpenSpread.entry_credit` is the filled order's LIMIT and runs 9-18% low, so
`credit_stop` is tight by the same margin"* — confirmed a third time, now at n=8 and
with a **realized dollar consequence attached rather than a projected one.** The
prohibition it carries is the one that matters most today: *no conclusion about
`credit_multiple` may be drawn from a dataset measured against that basis* — and
today's dataset is measured against exactly that basis, which is the second reason
the $1,550 cannot size anything.

**The recommendation this changes.** Capturing `FillInfo.fill_price` durably (column
+ migration) has been DEFERRED as a reporting improvement. It is not one. It is the
blocker on the largest P&L lever the system has, it makes every stop marginally
early, and today it is the direct cause of one of the eight positions being closed.
It should be re-scoped as a correctness fix on the exit path.

> **DONE 2026-09-02** (migration 0005), and one premise above needs correcting: the
> deferral rested on `store.py`'s claim that per-fill prices were absent in *"every
> case chunk 1 actually observed"*, which made this look like new machinery. They
> have been arriving all along — `_map_fills` → `OrderLeg.fill_price` → `_apply_leg`
> → `AppliedFill.price` → the `order.fill` records quoted in this very record. Only
> the DURABILITY at spread level was missing. The gap is also better read in
> absolute terms than as a percentage: **a half-tick to a tick of price improvement,
> every one of the five times it has been measured.** The prohibition NARROWS rather
> than lifts — conclusions only from `entry_credit_basis: realized` positions, a
> `limit` one EXCLUDED rather than corrected — and a number measured in cert still
> cannot size a change on its own, because a right basis does not make sandbox fills
> realistic.
> → [the credit stop's two wrong numbers](2026-09-02-the-credit-stop-basis-and-its-comparison.md)

### And the gate was saturated

```
GATE
  allowed                                     69
  denied: concurrent_limit                  2,642
  denied: account_stream_stale                  6
  denied: position_max_loss_exceeded            1
```

**97.3% of the day's gate decisions were denied on `concurrent_limit`.** At
`max_concurrent_positions = 2` the book was full essentially the whole session. Not a
defect — the cap is doing its job, and it is welded to `daily_max_loss` — but it
means the day is **cap-bound, not signal-bound**, and every observation about signal
quality today is made through a 2.7% aperture. The six `account_stream_stale` denials
are the residue of finding 8; the single `position_max_loss_exceeded` is the size
rail refusing one proposal, working correctly.

## The audit log answered both open questions, and one answer is a new defect

The first pass left two questions. Both are now closed against the chain, and the
first one turned out not to be a reporting artifact at all.

### An exit that SUCCEEDED was recorded as a failure — NEW, and the sharpest thing here

> **FIXED 2026-09-02**, designed first and signed off, shipped with finding 2.
> → [a terminal rung is not a verdict](2026-09-02-a-terminal-rung-is-not-a-verdict.md)

`put-7615-7610` closed cleanly. The fills are in the log:

```
618798 14:42:12 argus-close-20260901-put-7615-7610  Buy to Close  2  7615  4.50
618800 14:42:12 argus-close-20260901-put-7615-7610  Sell to Close 2  7610  3.00
```

Net debit 1.50, which is exactly what `desk` scored. And in the same second:

```
618797 14:42:12 order.event    reject_reason 'Received a fill on the original order'
                               broker_order_id 1430433
618801 14:42:12 walk.finished  outcome 'rejected'  last_status 'Rejected'
                               reason 'broker rejected the order'
                               reject_reason 'Received a fill on the original order'
                               broker_order_id 1430433  filled_contracts 0  steps 1
```

**Two different orders.** The exit walk opened at 1430426, repriced once, and the
broker refused the replacement **because the original had just filled** — and said so
verbatim. The spread was flat. The walk reported `REJECTED` → `ExitOutcome.NOT_RESOLVED`,
spent the position's one exit attempt, and paged `watchdog_exit_not_resolved`.

**The mechanism, confirmed in source.** `_reprice` (`loop.py:1081-1098`) moves
`state.broker_order_id` and `state.last` onto the replacement as soon as the replace
is *acknowledged*, and **never inspects `replaced.status`**. The next `_observe` reads
the replacement, finds it terminal, and `_resolve` (`loop.py:974-996`) takes its verdict
rung-scoped: not FILLED → `REJECTED` → `_finish`. `filled_contracts` is `0` because
`state.last` is the rejected replacement, which filled nothing; the original filled 2.

**The codebase already holds the rule this violates.** CLAUDE.md's non-negotiable:
*"The FILLED check alone stays identifier-WIDE, deliberately — a fill on any rung
means the spread is flat."* That rule is implemented in `_await_settled` on the cross
path and is **not** applied at `_resolve`'s terminal branch, where a walk that reprices
concludes from one rung about a spread that several rungs share. The `_recover` path
does ask the identifier-wide question, but it is unreachable here: the replace
succeeded, so nothing raised, and the rejection arrived through the ordinary
observation path.

**Rate: 1 of the 3 successful replaces today (33%).** Not a remote race — the replace
is sent precisely when an order has rested at a price long enough that a fill is
imminent, so the two events are correlated by construction.

**Cost today: none to safety, real to everything else.** The position was already
flat, so the spent attempt bought nothing and `_open_spreads` correctly stopped
watching it — which is why A2's `exit_spent_still_held` never fired and why the
position was absent from the 15:30 flatten scan. What it cost was a false page, a
spent attempt, and a mis-scored day (`desk` reports it `not_resolved`).

**The entry mirror is the one to think about.** `_reprice` is shared. An entry walk
whose original fills as the replace goes in reports `REJECTED`, `entry.py` pages
`entry_rejected`, and `entry.finished` records `outcome='rejected'` for a trade idea
that **opened a position**. That is not a safety event — `positions` is written from
the fill feed by chunk 2, independently of the walk's verdict, so the watchdog still
adopts it — but it corrupts `desk`/`mark` scoring, and it would feed a false count
into the open decision about backing off on **consecutive terminal rejections**. That
rail must not count a rejection that means "your order filled."

**And this is exactly what finding 2 cost.** `reject_reason` was populated all along —
`'Received a fill on the original order'` is sitting in `walk.finished`. The Slack page
said `"reason": "walk ended rejected: broker rejected the order"`. **The field the
notify dict drops is the one that would have told the operator the exit had
succeeded.** Finding 2 is no longer a legibility nicety; it is the difference between
a false alarm that reads as a false alarm and one that reads as a live problem.

### The broker relabelled closing legs — the never-fired condition, fired three times

The chained-book query returned the case
[relabelled closing legs](2026-08-25-relabelled-closing-legs-open.md) has been waiting
on since 2026-08-25, described there as *"not observed; needs an exit on a chained
pair, which has never fired."*

```
618920 14:43:24 argus-open-20260901-put-7600-7595   Sell to Close  2  7600  1.65
619083 14:45:02 argus-close-20260901-put-7605-7600  Sell to Open   2  7600  2.65
625229 15:31:32 argus-close-20260901-put-7600-7595  Sell to Open   2  7595  0.05
```

All three are the documented mechanism: tastytrade labels a leg against **the
account's** position, not Argus's intent. Opening `put-7600-7595` sold a 7600 the
account was long (from `put-7605-7600`'s long leg) → `Sell to Close` on an **opening**
order. Closing `put-7605-7600` sold a 7600 the account had netted flat → `Sell to
Open` on a **closing** order. Same for 7595.

**Two things this settles.** First, the branch that record says *"decides the fix"* —
whether tastytrade relabels or REJECTS `Sell to Close` on a flat position — is
answered: **it relabels.** Second, `infer_direction` (`models.py:644-658`) returns
`OPEN` on `any(leg.action.opens)`, and both closing orders above carry a `Sell to
Open` leg, so both infer **`open`** for a close. Its docstring's premise —
*"Argus never mixes opening and closing legs in one order"* — is true of what Argus
**sends** and false of what the broker **echoes**, and today is the counterexample:
`Buy to Close` and `Sell to Open` in one order.

Both were FULL closes, so the harm is the benign case that record predicts (the legs
cancel, `_pair_spreads` short-circuits before reading direction). **The condition is
now proven live, and the next PARTIAL close on a chained pair produces the
`malformed` half-spread** — one page, nowhere on `/status`.

**READ BACK 2026-09-03 — the durability is now observed, not inferred.** All three
closing order rows carry `direction = 'open'`:

```
1430448  argus-close-20260901-put-7605-7600   dir=open   Filled      lim=-1.25
1430542  argus-close-20260901-put-7600-7595   dir=open   Cancelled   lim=-0.05
1430543  argus-close-20260901-put-7600-7595   dir=open   Filled      lim=-0.10
```

The identifier says `close`, the column says `open`, and nothing can repair it later:
`direction` is absent from `_upsert_order`'s `ON CONFLICT` clause, so the value
`map_placed_order` inferred from the submission RESPONSE is what the row keeps for
life. That closes the last inferred link in this chain — see
[relabelled closing legs](2026-08-25-relabelled-closing-legs-open.md), where the same
read also records why this evidence had to come from the ledger rather than from the
legs.

### `replace_order` carries the REMAINDER — a standing open question, answered

`put-7630-7625`, the false-partial position, in full:

```
585440 14:39:01 walk.holding          1429812  limit 0.50
585448 14:39:07 order.replace_attempt          limit 0.45
585450 14:39:08 order.fill    1429812  1  Sell to Open  2.05
585451 14:39:08 order.fill    1429812  1  Buy to Open   1.55
585461 14:39:13 order.replaced 1429813         limit 0.45
585474 14:39:16 order.fill    1429813  1  Sell to Open  2.15
585475 14:39:16 order.fill    1429813  1  Buy to Open   1.65
585488 14:39:21 walk.finished  1429813  filled_contracts 1  requested_contracts 2  steps 1
```

The original filled **1** contract; the replacement filled **1**. Total held: **2**.
Had the replace carried the original quantity, the position would be 3. So chunk 3's
open question — *"whether the new order carries the original quantity or the
remainder"* — is answered: **the remainder.** The failure it was written to avoid
("re-request the full size and end up with double the intended position") does not
occur.

Two more measurements fall out of the same rows:

- **`entry_credit` is understated, now proven from fill prices rather than inferred.**
  Rung 1 filled 2.05/1.55 = **0.50**; rung 2 filled 2.15/1.65 = **0.50** against a
  0.45 limit (a tick of price improvement). Both contracts collected 0.50; the
  watchdog stopped against 0.45. `desk`'s reconstruction was right, and the stop that
  fired at 0.925 against 0.90 would not have fired against the true 1.00.
- **`watchdog.check` shows `contracts=1` from 14:39:09 to 14:39:16 and `contracts=2`
  after.** The watchdog was live on a 1-contract spread for 8 seconds while the
  remainder worked — correct, defined-risk, and good evidence that position tracking
  follows fills rather than the walk's report.

So finding 5's mechanism is **1, not 2**: not snapshot lag, but `filled_contracts`
being scoped to one rung of a walk that filled across two.

## Findings

### 1. The flatten allowance is spent before the order can actually be built

At 14:30 the flatten fired on both held spreads. Both produced:

```
watchdog_exit_unattempted … "reason": "ValueError: net mid 0.0 rounds to 0.00
at a 0.05 tick — there is no valid limit price for this spread"
```

`_limit_price` (`execution/build.py:246`) refuses a mid that rounds to zero, and it
is right to: whether it is worth paying a tick to close a worthless spread is an
exit-policy decision, not a builder's. The exit correctly reported `NOT_ATTEMPTED`,
and `NOT_ATTEMPTED` explicitly restores the position's ordinary attempt —
*"the position keeps its one attempt and the trigger stays latched"*
(`watchdog.py:2944-2947`). Both positions later crossed and filled. Today: harmless.

**The defect is one allowance over.** `watchdog.py:2910-2919` spends the flatten's
one-shot past-the-mark override at the handoff:

```python
if TriggerReason.FLATTEN_BEFORE_CLOSE in fired:
    # Spend the flatten allowance HERE — at the handoff, and only once an
    # order can actually be built. …
    self._flatten_attempted.add(key)
```

The comment's reasoning is right and its placement does not implement it. The spend
sits after the `evaluation.market is None` guard — i.e. once the legs have **quotes**
— but `build_order` can still fail *after* that, and the way it fails is
`_limit_price` on a net mid of zero. So:

- **`_attempted` is restored on `NOT_ATTEMPTED`. `_flatten_attempted` is not.**
  Two allowances, one failure mode, opposite treatment.
- **This is the flatten's modal regime, not its tail.** A winning 0DTE spread at
  15:30 is nearly worthless, which is what makes it winning. A mid that rounds to
  0.00 at a 0.05 tick is the *expected* first look, and today it was the observed
  one on 2 of 2 flattens.
- The position this costs is precisely the one the override exists for: a spread
  arriving at 15:30 with its ordinary attempt already spent. It gets a single
  unpriceable look, the override is consumed by an order that was never sent, and
  nothing tries again for the rest of expiry day.

Today's two positions escaped only because neither had spent its ordinary attempt.
That is a property of the day, not of the mechanism.

Secondary, and cosmetic by comparison: `reason` on the operator page is a
stringified `ValueError`. It is accurate, and it reads as a crash rather than as the
refusal it is.

### 2. The exit alert drops the broker's rejection prose — CONFIRMED

> **FIXED 2026-09-02**, shipped with the finding above. The sweep it prompted found a
> third page (`watchdog_flatten_override`) with the same gap.
> → [a terminal rung is not a verdict](2026-09-02-a-terminal-rung-is-not-a-verdict.md)

**Now confirmed against the chain, and it is worse than it looked.** The dropped
string was `'Received a fill on the original order'` — the sentence that says the exit
had already succeeded. See the new-defect section above for what that cost.

At 13:42 `put-7615-7610` triggered on `underlying_near_short_strike` and its exit was
**REJECTED** — the first exit rejection in the system's history. The page said:

```
watchdog_exit_not_resolved … "reason": "walk ended rejected: broker rejected the order"
```

That string is the hardcoded fallback at `loop.py:989`. P6 (2026-08-29) added
`WalkResult.reject_reason` specifically so *"an operator need not correlate against a
separate `order.event` record by hand"*, and `_finish` does populate it
(`loop.py:1286-1290`). It reaches the Tier 1 audit record — `ExitReport.to_payload()`
includes `"walk": self.walk.to_payload()`, which carries `reject_reason`.

It does **not** reach Slack. `_record_exit` (`watchdog.py:1359-1370`) hand-builds a
six-key notify dict — `external_identifier`, `candidate_id`, `contracts`,
`entry_credit`, `trigger_reasons`, `reason` — and `walk` is not among them. So P6
landed on the entry path (`entry_rejected` alerts `payload`, which is the walk's) and
silently missed the exit path, and the first exit rejection ever paged with a
constant while the broker's own words sat in the audit log.

The fix is one key. The scoping rule from B1 is already satisfied — `candidate_id` is
at the top level of that dict.

### 3. The exit ladder collapse reproduced, twice

Both flattens logged `walked_away_at: -0.05` and then crossed at `-0.10`:

```
watchdog_crossed_market [argus-close-20260901-put-7600-7595]
  {"crossing_limit": -0.1, "walked_away_at": -0.05}
watchdog_crossed_market [argus-close-20260901-put-7595-7590]
  {"crossing_limit": -0.1, "walked_away_at": -0.05}
```

Identical to 2026-08-31: at `exit_walk_away_pct = 0.20`, a 0.05 opening debit floors
at 0.05, so the ladder has **zero range** and contributes nothing. Both outcomes rode
entirely on the market-crossing limit.

This is independent confirmation on two more positions, and it is the better outcome
of the two possible ones — on 08-31 the same collapse produced one fill and one
`no usable crossing price` that spent the attempt in 1.3s. Today both found an offer.

`watchdog.exit_walk_min_steps = 1` is built and unreleased on `main`. **Nothing
about today argues against it and two more rows argue for it.** It remains the
coupled-config deploy.

### 4. Entry `NO_ORDER_EXISTS` cost 3 of 11 trade ideas — now measured

Three candidates (`call-7685-7690` 09:34, `put-7635-7630` 09:35, `put-7610-7605`
12:26) ended:

```
"outcome": "no_order_exists", "steps": 0, "broker_order_id": null,
"recovered_disconnects": 1,
"reason": "submit failed and no order exists under this identifier"
```

The submit threw, `_recover` asked the broker, and the broker positively confirmed
nothing exists. On the **exit** path that state is now retried (P0, 2026-08-28) under
`_EXIT_SEND_ATTEMPTS`, on the reasoning that a confirmed-empty broker is the safest
thing there is to send into. On the **entry** path it is terminal: `_attempted`
claimed the candidate before the guard ran, `_ALERTING_OUTCOMES` pages, and the trade
idea is never re-proposed.

**That asymmetry is deliberate and documented** (`entry.py:215-221`: *"a submit that
failed with nothing at the broker costs a trade and leaves no exposure"* — versus the
exit side, where not retrying leaves a held position with no stop). This finding does
not dispute it. It supplies the number the reasoning never had: **27% of the day's
trade ideas, on a day with `walk.disconnected: 7` and 498 account-refresh failures.**

Worth carrying: the entry-side cost of that rule is now a measured quantity on a bad
cert day, and it should be re-measured on a clean one before anyone argues from it in
either direction.

### 5. A false `entry_partially_filled` page — RESOLVED, mechanism 1

**The audit chain settled this** (section above): the replace carried the remainder,
each rung filled 1 contract, the trade idea holds 2, and `filled_contracts` reported
one rung's fill against the whole idea's request. Not snapshot lag. The original
reasoning is kept below because the rule it derives is the durable part.

At 09:39 `put-7630-7625`:

```
entry_partially_filled … "filled_contracts": 1, "requested_contracts": 2,
"outcome": "filled", "last_status": "Filled", "steps": 1, "reason": null
```

`reason` is `null`, so this did **not** go through the partial-fill branch
(`loop.py:998-1014` sets `reason="partial fill; remainder cancelled"`). It went
through `loop.py:983-986` — a terminal FILLED observation. And the position was
subsequently exited at **2 contracts** (`watchdog_exit_filled … "contracts": 2`,
where `contracts` is `spread.contracts`, sourced from `positions`, which is written
from the fill feed). No `imbalanced` flag on any later spread payload.

So the trade idea holds 2 and the walk reported 1. `WalkResult.filled_contracts` is
`state.last.reported_fill_quantity` — max per-leg `filled_quantity` on **one broker
order**, the current rung. `steps: 1` means one reprice happened. Two mechanisms fit:

1. **`replace_order` carries the REMAINDER.** Rung 1 fills 1 of 2, the replace
   requests 1, the replaced order fills 1 and reports `Filled` with per-leg quantity
   1. Total 2. `state.sent.contracts` is still the walk's original 2, so
   `partially_filled` compares one rung's fill against the whole idea's request.
2. **Snapshot lag inside the terminal observation.** The loop read `Filled` while
   the legs had booked only 1 of 2 — 2026-08-31 measured a 3.07s leg-booking window
   on a 2-leg order.

Either way the defect class is the same and is worth stating as a rule:
**`filled_contracts` is scoped to one broker order and is not authoritative about
what a trade idea filled — `positions` is.** It is the same walk-versus-identifier
distinction already welded into the cross confirmation ("a walk reprices, several
rungs share one `external_identifier`"), applied to fill quantity instead of
disposition. `partially_filled` straddles the two scopes and pages on the mismatch.

Blast radius today: one false page. `filled_contracts` is read by `tools/desk.py` for
display and by nothing that decides anything.

**If mechanism 1 is what happened, today answered a standing open question** — *"what
does `replace_order` do to a PARTIALLY FILLED order, carry the original quantity or
the remainder?"* — which is the reason chunk 3 cancels a partial's remainder rather
than walking it. That question is worth closing on evidence, not on this inference;
the query is below.

### 6. One failed order poll blinds two readers

`watchdog.working_orders_unread: 66`. Each is a failed `get_live_orders()` inside
`_refresh_working_orders`, which by design swallows the exception and returns
(`watchdog.py:2649-2660`). One fetch, two readers: `open_working_spread_equivalents`
(P4's third term on the concurrency cap) and `_recheck_residual_orders` (P5). Both
sat out 66 ticks.

Both behaviours are correct as designed and documented — the count fails toward
blocking an entry, and absence is never a verdict for a residual. **The finding is
the volume, not the design.** 66 blind ticks on the day P5's registry first mattered
is worth knowing before `unmanaged_order_cancel` is promoted to `true`, because the
recheck's rate bound is measured against ticks that happen, and 66 of them did not.

### 7. RSS grew 80 MB in 51.7 hours

`rss_at_start_bytes` 109,150,208 → `rss_bytes` 189,165,568, or
`rss_growth_bytes_per_hour: 1547680` — ~1.5 MB/h, ~37 MB/day, ~1.1 GB/month on an
always-on process.

Not diagnosed here, and one number argues against the obvious guess:
`gc_collections` gen2 is **2** over 51.7 hours (`gc_gen2_per_hour: 0.0387`). The
per-session state that could accumulate — `_attempted`, `_book_confirm`,
`_impaired`, `_spent_marks`, the residual registry — is all built in
`_run_session_day` (`session.py:735-802`) and dropped at the rollover, so a
per-session leak would show as **step** growth at the day boundary rather than the
smooth ramp reported. Cyclic garbage from the retired session graph simply not having
been collected yet fits the gen2 count.

Worth a deliberate look before the live flip, because the shape of the answer decides
whether it matters: two retired sessions pending collection is a non-issue; a ramp
that survives a forced `gc.collect()` is not.

**And the field named here is not a safe instrument for that look.** The addendum
below records that `rss_growth_bytes_per_hour` and `gc_gen2_per_hour` divide a fixed
startup cost by uptime, so they read ~30x high for the first hours of a process and
only settle toward the truth after many. The 1.5 MB/h figure above is from a 51.7 h
sample and is sound; a reading taken shortly after a restart is not. Measure from
`rss_bytes` sampled hours apart. → [addendum](#the-status-per-hour-rate-fields-are-startup-contaminated),
[full write-up](2026-09-01-status-per-hour-rates-startup-contaminated.md)

### 8. Fourteen stream disconnects, one reconnect

`account.stream_disconnected: 14` against `account.stream_reconnected: 1`. Not a
counting bug: `stream.py:504-509` emits `reconnected` only after `subscribe_accounts`
succeeds, while a *failed* reconnect attempt raises and emits another `disconnected`.
So the day held one successful reconnection and thirteen further link losses or
failed attempts, under doubling backoff.

Entries flowed throughout (11 walks, 2714 gate decisions), which is the REST liveness
refresh doing its documented job of backstopping the stream — and is also why
`account_stream_max_age_s` sits at 120 as a **cert-only** value. No action; recorded
because "the stream was down most of the day and nothing noticed" is a sentence that
should be written down rather than rediscovered.

### 9. `cost_to_close: 0.0` on a spread that cost 0.10 to close

The flatten trigger for `put-7600-7595` fired with `"cost_to_close": 0.0`, and the
position closed by crossing at a **0.10 debit**. This is not a new finding — it is
CLAUDE.md's existing rule (*"`cost_to_close` is a MARK, never a fillable price"*)
producing a clean instance of itself, one screen away from a page reporting the mark.
Recorded because it is the second consecutive flatten day to produce one, and because
finding 1's `net mid 0.0` is the same number arriving at the builder.

## Reproducing the chain reads

Both first-pass questions are answered in the sections above. The commands are kept
because they are the shape any future pass wants, and because the second one is the
instrument that settles a replace question.

Read-only URI, so neither takes a writer lock and both are safe alongside the live
process. `-T` because `compose run` allocates a TTY by default and that breaks the
heredoc. `python -` rather than `sqlite3` because there is no sqlite CLI in the image.

**Chained-leg attribution, relabelling, and the rejected exit's real reason:**

```bash
sudo docker compose -f /opt/argus/docker-compose.yml run --rm -T argus python - <<'PY'
import sqlite3, json
db = sqlite3.connect("file:/data/argus.db?mode=ro", uri=True)
CHAIN = ("7615-7610", "7605-7600", "7600-7595", "7620-7615")
print("=== FILLS on the chained candidates: who was each leg attributed to? ===")
print("Look for a CLOSING identifier carrying an 'to Open' action (or the reverse):")
print("that is the 2026-08-25 relabelling, on the exit side, for the first time.\n")
print(f"{'seq':>7}  {'et':<8} {'attributed to':<40} {'action':<16} {'qty':>3} {'strike':>7} {'price':>7}")
for seq, ts, pl in db.execute(
    "SELECT seq, ts, payload FROM audit_log "
    "WHERE category='order.fill' AND ts>='2026-09-01' ORDER BY seq"
):
    p = json.loads(pl)
    ident = str(p.get("external_identifier") or "")
    if not any(c in ident for c in CHAIN):
        continue
    sym = str(p.get("symbol") or "")
    strike = sym[-8:].lstrip("0")[:-3] or "?"
    price = p.get("price")
    print(f"{seq:>7}  {ts[11:19]} {ident:<40} {str(p.get('action')):<16} "
          f"{p.get('contracts'):>3} {strike:>7} {('-' if price is None else f'{float(price):.2f}'):>7}")

print()
print("=== The rejected exit: what did the broker actually say, and what direction ===")
print("=== was recorded for the CLOSING order?                                    ===")
for seq, ts, cat, pl in db.execute(
    "SELECT seq, ts, category, payload FROM audit_log WHERE ts>='2026-09-01' "
    "AND category IN ('walk.finished','order.event','order.submitted','order.submit_failed') "
    "ORDER BY seq"
):
    p = json.loads(pl)
    if "7615-7610" not in str(p.get("external_identifier") or ""):
        continue
    print(f"  seq={seq} {ts[11:19]} {cat}")
    for k in ("outcome", "last_status", "reason", "reject_reason", "direction",
              "broker_order_id", "filled_contracts", "requested_contracts", "steps"):
        if k in p:
            print(f"      {k:<20} {p[k]!r}")
PY
```

**One walk's full record, rung by rung — the replace-semantics instrument:**

```bash
sudo docker compose -f /opt/argus/docker-compose.yml run --rm -T argus python - <<'PY'
import sqlite3, json
db = sqlite3.connect("file:/data/argus.db?mode=ro", uri=True)
IDENT = "20260901-put-7630-7625"
print(f"=== Every record for {IDENT}, in chain order ===")
print("DECISIVE: `contracts` on order.replaced.  2 = the replace carried the")
print("          ORIGINAL quantity; 1 = it carried the REMAINDER.")
print("          Then count leg-contracts filled: a 2-leg vertical books 2 leg")
print("          movements per contract, so a full 2-contract spread = 4.\n")
legs = 0
for seq, ts, cat, pl in db.execute(
    "SELECT seq, ts, category, payload FROM audit_log WHERE ts>='2026-09-01' ORDER BY seq"
):
    p = json.loads(pl)
    if IDENT not in str(p.get("external_identifier") or ""):
        continue
    bits = [f"{k}={p[k]!r}" for k in (
        "broker_order_id", "contracts", "limit_price", "new_limit_price", "action",
        "price", "outcome", "filled_contracts", "requested_contracts", "steps",
        "final_limit_price", "last_status") if p.get(k) is not None]
    print(f"  seq={seq} {ts[11:19]} {cat:<24} " + "  ".join(bits))
    if cat == "order.fill":
        legs += int(p.get("contracts") or 0)
print(f"\n  total leg-contracts filled under this identifier: {legs}")
print("  4 => the spread really is 2 contracts and `filled_contracts` undercounts;")
print("  2 => only one contract ever filled and `positions` is the thing that is wrong.")
PY
```

Note the second one matches on the candidate stem, so it spans the OPEN and the CLOSE
identifiers both; the leg-contract total counts entry and exit together (8 for a
2-contract vertical round trip, not 4).

**RUN 2026-09-03 — the last owed read, and it is closed.** What `direction` was
actually written to the `orders` table for `argus-close-20260901-put-7605-7600` and
`argus-close-20260901-put-7600-7595`: **`open`, on all three closing rows** (above).

The query, and the two things running it taught that the query itself did not:

```bash
cd /opt/argus && sudo docker compose exec -T argus python - <<'PY'
import sqlite3
c = sqlite3.connect("file:/data/argus.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print("order_legs rows total:", c.execute("SELECT count(*) FROM order_legs").fetchone()[0])
for r in c.execute("""
    SELECT o.broker_order_id AS id, o.external_identifier AS xid, o.direction AS dir,
           o.status AS st, o.limit_price AS lim, count(l.symbol) AS nlegs,
           group_concat(l.action, ' | ') AS actions
      FROM orders o LEFT JOIN order_legs l ON l.broker_order_id = o.broker_order_id
     WHERE o.external_identifier LIKE '%put-7605-7600'
        OR o.external_identifier LIKE '%put-7600-7595'
     GROUP BY o.broker_order_id ORDER BY o.recorded_at
"""):
    print(f"{r['id']} {r['xid']:<40} dir={r['dir']:<5} st={r['st']:<16} "
          f"lim={r['lim']:>6} legs={r['nlegs']} [{r['actions']}]")
PY
```

`exec`, not `run --rm`, on purpose: it uses the container that is already up, so there
is no second process and no `.env` re-read. `mode=ro` means it cannot touch the chain
or contend with the writer lock.

**`order_legs` came back EMPTY — 0 rows in the whole table — and that is the sandbox
reset working, not a defect.** `_clear_reset_holdings` deletes `order_legs` alongside
`positions` and deliberately keeps `orders`
([sandbox reset](2026-08-17-sandbox-reset-order-ledger.md)), which is exactly the shape
observed two days later: 09-01 order rows standing with no legs under them. Anyone
re-running a leg-level query against a past day in cert will find the same thing, and
it is not evidence of a booking failure.

**The consequence for THIS chain: the broker's verbatim leg actions are not durable in
the ledger.** They are wiped every morning the reset fires, so `orders.direction` is
the only surviving ledger trace of a relabelling — and the audit log, which is
hash-chained and never wiped, is the only durable source for the leg actions
themselves. `order.submit_attempt` carries the order as Argus BUILT it and
`order.submitted` carries it as the broker RETURNED it, so those two records side by
side show the relabelling per leg at the instant it happens. **Read `order.submitted`
through its `"order"` key** — it records `OrderAck.to_payload()`, which nests the order
one level down, so a flat `payload["legs"]` reads empty and looks like a missing record
when it is a shape mismatch.

That pair was then read for all four orders, and it is the strongest artifact this
chain produced — the transformation itself, sent against returned, per leg. Tabulated
in [relabelled closing legs](2026-08-25-relabelled-closing-legs-open.md). The two lines
that matter here: **`direction` is already wrong at `order.submitted`**, which is the
record `record_order` writes from, so the row was never right for an instant; and
**`order.submit_attempt` holds the correct `direction = close` at Tier 1**, three
seconds earlier, so the true intent is durable and the column is repairable from the
chain if a backfill is ever wanted.

## What today does not prove

Everything CLAUDE.md already says about cert, unchanged. Orders match inside
tastytrade's own sandbox, so **the two crossing fills at −0.10 are not evidence that
a real book would have paid it** — and finding 3's argument for `exit_walk_min_steps`
rests on the ladder's arithmetic having zero range, which is true regardless of who
filled.

Still not exercised: an order that **stays** at `Cancel Requested`. Today added two
`order.cancel_requested` and two `order.cancel_attempt` with three residuals
registered and two resolved — the benign fast case again, not the stuck one.

The daily max-loss breaker remains structurally inert (`daily_loss_breaker.tripped:
false` against a day P&L that is zero by construction). Nothing today touches that
LIVE-FLIP GATE.

## Recommended order

1. ~~**The reprice-rejection read.**~~ **DONE 2026-09-02 — designed, signed off and
   built the same day, `src/` only.** `_resolve`'s terminal-non-fill branch now asks
   `by_identifier` before it concludes, ungated on `steps`, `CANCELLED_ELSEWHERE`
   included, with `state.last`/`state.broker_order_id` moving to the filled rung and a
   probe that cannot be answered falling back to the verdict the branch already had.
   The rule applied is the one the codebase already stated and already implemented one
   path over: **the FILLED check is identifier-wide.**
   → [a terminal rung is not a verdict](2026-09-02-a-terminal-rung-is-not-a-verdict.md)
2. ~~**Finding 2 — the notify dict.**~~ **DONE 2026-09-02, shipped with (1)** — they
   are the same incident seen from two surfaces, and fixing the read without the page
   would have left the next occurrence as illegible as this one. `_record_exit`,
   `_page_if_spent_and_still_held` and `_flatten_may_act` now carry `reject_reason`,
   and the rule (*an alert that names an exit attempt's outcome carries the broker's
   reason for it*) is pinned by an AST sweep — which is what found the third one.
3. ~~**The float-epsilon stop.**~~ **DONE 2026-09-02 — designed, signed off and built
   the same day, `src/` only, shipped as its own commit ahead of (4).** The
   comparison now requires an observable margin (`PRICE_EPSILON`, derived from
   `zerobid._INPUT_PRECISION`). **The decision this record framed — "`Decimal`
   comparison versus a half-tick tolerance" — turned out to be a false choice**:
   `float(Decimal("0.8")) * 2.0` is exactly the double nearest 1.6, so the precision
   is not lost at the multiplication and `Decimal` fixes nothing; and a half-tick
   tolerance is the size of a real price move and would retire genuine breaches. The
   noise is upstream in `spread_mid`. A live specimen turned up in the test suite:
   23 watchdog tests were passing on a 2.2e-16 breach.
4. ~~**Re-scope `FillInfo.fill_price`.**~~ **DONE 2026-09-02, shipped as a second,
   independent commit carrying migration 0005.** `entry_credit` is now the realized
   credit, aggregated per leg across every opening order in the trade idea and
   contracts-weighted — because this day proved a walk fills across several orders
   under one identifier. **This record's own premise about the cost was the thing
   that needed correcting**: `store.py` claimed per-fill prices were absent in "every
   case chunk 1 actually observed", and they have been arriving all along — only the
   durability at spread level was missing. The gap is a half-tick to a tick, so the
   stop moves later by $5-$10 a contract and no size rail moves.
   → [the credit stop's two wrong numbers](2026-09-02-the-credit-stop-basis-and-its-comparison.md)
5. ~~**Finding 1** — the flatten allowance, spent before the order can be built.~~
   **DONE 2026-09-02 — designed, signed off and built the same day, `src/` only, so the
   image deploys alone.** The spend stays at the handoff as the fail-closed default and
   `ExitOutcome.NOT_ATTEMPTED` gives the allowance back — the same signal `_attempted`
   has always been read by, one field over. **This record's framing of the choice as
   "where should the spend go" had a third answer it did not list and that is the one
   that shipped**: not at the handoff and not inside `close()`, but at BOTH ends — a
   fail-closed claim and a positively-signalled release, so a crash cannot grant and only
   a returned value can restore. **Shipped with a second defect the design pass found and
   measured**, which this record missed because the first bug was hiding it:
   `watchdog.flatten_override` paged once per TICK rather than once per position, 8 Tier
   1 records for 8 ticks, and giving the allowance back un-masks and widens it.
   → [the flatten's override is spent by a send](2026-09-02-the-flatten-allowance-is-spent-by-a-send.md)
6. **Deploy `exit_walk_min_steps`** with the coupled config (finding 3), already built
   and waiting, with two more rows of evidence behind it.

**Update `2026-08-25-relabelled-closing-legs-open.md` either way**: its "never fired"
premise is now false and its unresolved branch is resolved — tastytrade relabels, it
does not reject. Whether to *fix* `infer_direction` is still a decision, but it is now
a decision with data instead of a hypothetical.

Findings 6–9 (poll blindness, RSS, stream disconnects, the mark-versus-fill note) are
observations to carry, not work to schedule.

**Written up as prompts**, each design-gated before implementation:

| Covers | Prompt |
|---|---|
| ~~(1) + (2) — the reprice-rejection read and the dropped `reject_reason`~~ **SHIPPED 2026-09-02** ([design](../tasks/argus_reprice_rejection_identifier_wide_design.md), [record](2026-09-02-a-terminal-rung-is-not-a-verdict.md)) | [`argus_reprice_rejection_identifier_wide_prompt.md`](../tasks/argus_reprice_rejection_identifier_wide_prompt.md) |
| ~~(3) + (4) — the float-epsilon stop and the `entry_credit` basis~~ **SHIPPED 2026-09-02** ([design](../tasks/argus_credit_stop_wrong_numbers_design.md), [record](2026-09-02-the-credit-stop-basis-and-its-comparison.md)) | [`argus_credit_stop_wrong_numbers_prompt.md`](../tasks/argus_credit_stop_wrong_numbers_prompt.md) |
| (5) — the flatten allowance | [`argus_flatten_allowance_spent_early_prompt.md`](../tasks/argus_flatten_allowance_spent_early_prompt.md) |

(6), deploying `exit_walk_min_steps`, needs no prompt — the code is built and the
change is a coupled config deploy.

---

## Addendum — 2026-09-01 evening: v0.3.14 deployed, and the persistence rail proved itself across the version boundary

**v0.3.13 (Tejat) was replaced by v0.3.14 (Grumium) at 17:02 EDT the same day**,
after the review pass above. Deploy carried the coupled config key
(`watchdog.exit_walk_min_steps = 1`) and three `src/`-only changes — the near-tick
exit ladder fix, `_end_entries_today`, and kill-switch persistence + `POST /resume`.
Nothing else about the environment changed. This addendum records only what the
first boot of the new image showed, because one item is a genuine
proof-first-time and one is a small defect in an instrument this record already
leans on.

### Kill-switch persistence: proven end to end, and across a code-version boundary

The first process on the new image rehydrated the kill switch from the audit log
and came up **ENGAGED**, restoring a trip that four sessions had already traded
straight through.

```
seq 496115  kill_switch.trip       2026-08-28 12:15:48 ET   ← written by the PRE-v0.3.14 image
                                   source: reconciliation_mismatch
                                   reason: "broker reconciliation mismatch (1 discrepancy(ies))"
seq 625663  process.start          2026-09-01 17:02:24 EDT  ← v0.3.14
seq 625664  kill_switch.restored   trip_seq: 496115, trip_ts preserved
seq 625666  reconcile.result       ok: true, both discrepancy lists empty
...
seq 625667  kill_switch.resume     2026-09-01 17:09:15 EDT
                                   cleared_reason / cleared_source carried
                                   detail: {peer: 192.0.2.11, restart_required: false, halt_grade: null}
```

That trip is **the 2026-08-28 12:15 ET halt** — the one whose single discrepancy
was abandoned order 1424563, the incident P3/P5 were built for. It had sat
un-cleared in the audit log for four days because `kill_switch.resume` did not
exist until this release, and every process since — 2026-08-31 (2 positions),
2026-09-01 (8 positions, −664.54 realized) — had booted `_engaged = False` and
traded through it. **The rail was being silently ignored across both live-fire
days this record and its predecessor analyse.**

Every design property held on that first boot:

- `kill_switch.restored` at Tier 1, `trip_seq` / `trip_ts` pointing at the
  original trip — **not** a second `kill_switch.trip`.
- exactly one page (the CRIT+WARN pair at 17:02:24,874).
- `/status` rendered `kill_switch.engaged: true` with `since` = the **original**
  2026-08-28 press time and `restored: true` — i.e. `ENGAGED (RESTORED)`, not a
  green page and not a fresh timestamp.
- `halted.halted: false`, `grade: null` — rehydration restored the **switch** and
  never `HaltRecord`, so `restart_required` came back `false` and no second
  restart was needed.
- the same boot's `reconcile.result` was `ok: true` with both discrepancy lists
  empty, confirming the originating condition was long resolved — the halt was
  stale, not live.

`argus-resume` (`POST /resume?confirm=yes`) then cleared it in one call:
`{"status": "resumed", "restart_required": false, "halt_grade": null}`, seq 625667
written with the peer IP and no token, `cleared_reason`/`cleared_source` carried so
the trail says *what* was cleared. Follow-up `/status`: `engaged: false`,
`restored: false`, all attribution fields null.

**What this proves that the design could only assert:** the trip was written by
the pre-v0.3.14 image and read back correctly by the new one. "The halt is state
of the world, not state of a process" was exercised against an actual process
boundary *and* a code-version boundary on day one.

**Still exercised only by the 33 unit tests + 4 mutations**, not in cert:
`kill_switch.restore_failed` (unreadable state → fail closed to ENGAGED);
`restart_required: true` (needs a live `HaltRecord` at resume time);
the `not_engaged` idempotent no-op and the 400 on a missing `?confirm=yes`. None
is the load-bearing promise, and the live-flip blocker is closed on the strength
of the boot above.

**One-time cutover hazard, now documented in the release notes.** The persistence
read has no date bound (deliberate — a Friday `/kill` must hold on Monday), so the
first boot of the image evaluates *the entire history* of `kill_switch.trip`
against a `resume` category that never existed before it. Any un-reconciled trip
ever written comes back engaged on that first boot. Check for a standing trip
*before* the cutover restart:
`sqlite3 "file:argus.db?mode=ro" "SELECT seq, ts, payload FROM audit_log WHERE
category = 'kill_switch.trip' ORDER BY seq DESC LIMIT 1;"`. From the second deploy
on, the backlog is gone.

### The `/status` per-hour rate fields are startup-contaminated

Noticed in the same two `/status` pulls (uptime 81 s and 447 s). RSS was flat
between them (`rss_growth_bytes` byte-identical at 5,345,280) and gen2 collections
held at 2, yet at 447 s the process advertised `rss_growth_bytes_per_hour:
43,032,517` (≈ 43 MB/h) and `gc_gen2_per_hour: 16.10`. The real sustained figures
from a 51.7 h run are 1.5 MB/h and 0.039 gen2/h — finding 7's numbers.

The cause: both fields divide a **fixed startup cost** by uptime, so they carry a
bias term `startup_bump / uptime` that is ~30x the signal for the first hours and
decays continuously — `_MIN_UPTIME_FOR_RATE_S = 120.0` suppresses the field below
120 s and cannot separate the two quantities above it. Finding 7 names
`rss_growth_bytes_per_hour` as the number to watch before the live flip; it is
reliable at the 51.7 h timescale that finding used and unreliable at the timescale
an operator reads it just after a deploy. **Measure from `rss_bytes` sampled hours
apart, not from the derived field.** Full write-up and fix options:
→ [`/status`'s per-hour rate fields are startup-contaminated](2026-09-01-status-per-hour-rates-startup-contaminated.md)

*Amended 2026-09-18:* **FIXED in v0.4.4.** Both fields are now a slope over the trailing
hour, published with `rate_window_s` and `null` for a process's first hour, so after
that deploy the derived field *is* the instrument to read.
