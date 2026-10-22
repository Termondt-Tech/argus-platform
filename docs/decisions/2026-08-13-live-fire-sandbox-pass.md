# The live-fire sandbox pass (2026-08-13)

*The first real order routed end to end — and the six things it found that no
amount of shadow or dry-run running could have.*

> Normative — do not relitigate. See [`docs/decisions/README.md`](README.md) for
> the index.


**v0.3.0 flipped `[execution] dry_run_submissions = false` against
`environment = "sandbox"`**, alongside `audit.synchronous = "FULL"` (the config's
own justification for `NORMAL` — "shadow submits nothing and holds nothing" — had
stopped being true). Config-only release: both commits touch `argus.toml` and
nothing else, so it deployed as a bind-mount refresh plus `systemctl restart`, no
image rebuild. Restarted 09:18 ET, 22 minutes before `session_open`.

## What ran

One complete round trip, `20260813-call-7825-7830`, 2 contracts:

| UTC | Event |
|---|---|
| 14:00:08.9 | first entry's legs booked into the ledger |
| ~14:21:5x | `watchdog.triggered` — the virtual stop, on `credit_multiple = 2.0` |
| 14:21:56.9 | `order.replace_attempt`, walk step 4 |
| 14:21:57.3 | `order.replaced` → **new** broker order id 1311952, limit −1.30 |
| 14:21:57.7 | prior order 1311951 → `Cancelled` / `dead` (~404ms after the replace) |
| 14:21:58.0 | 1311952 → `Routed` / working |
| 14:21:58.3 | 1311952 → `Live` / working |
| 14:21:58.5 | `order.fill` Buy to Close, 2 contracts |
| 14:21:58.7 | `order.fill` Sell to Close, 2 contracts (~140ms after the first leg) |
| 14:21:59.7 | `watchdog.exit_finished`, outcome `filled` |

**PROVEN:** construction → submission → `Routed`/`Live` → per-leg fills → ledger
booking → watchdog adoption → virtual-stop trigger → exit walk → fill. The walk's
reprice cycle issues a **new broker order id per step** and the prior order goes
terminal on its own. The exit filled inside its walk-away
(`cross_broker_order_id: null` — the market-crossing escalation was never needed).

**NOT EXERCISED, and still unmeasured:** partial fills, rejections, the
market-crossing escalation, and an explicit `cancel()` → `Cancel Requested` →
terminal transition. The 404ms above is *replace-driven* cancellation and is not
an answer to the `exit_cancel_settle_s` question — do not cite it as one.

## Finding 1 — `max_concurrent_positions` counts LEGS, not spreads

**`2` permits exactly ONE vertical.** Of 404 gate decisions, 402 were denied and
every one carried the single reason `concurrent_limit`. The two allowances are the
only two moments the book was flat: session start, and the ~10s gap after the exit
before the replacement entry booked.

The chain, verified from source:

- `AccountSnapshot.open_position_count` is
  `sum(1 for p in self.positions if p.signed_quantity != 0)` — one per OCC symbol,
  i.e. **per leg** (`account/models.py`).
- `gate.py` compares that directly:
  `if snapshot.open_position_count >= self._max_concurrent`.
- A vertical is two legs, so one open spread saturates a limit of 2.

**This was half-documented already.** [The unwatched-position gap](2026-08-11-unwatched-position-gap.md)
records both facts separately — that the units are non-flat LEG positions ("comparing
against `len(scan.spreads)` would have been wrong by 2x"), and that the gate's
concurrent check is one of the counter's two consumers — without ever connecting
them. The leg units are *correct* for the book-agreement check they were written
for. They are wrong for the gate.

**The real defect is that one risk key out of three is leg-scoped.**
`max_loss_per_position` reads `proposal.max_loss` and `max_contracts_per_position`
reads `proposal.contracts`; both are per-spread. Same word, two meanings.

**FIXED, with the exposure change explicitly declined.** Two paths existed, and
the choice was a risk decision rather than a bug fix:

1. **Group legs into spreads** at the gate. Correct, and it **doubles effective
   exposure** the moment it lands.
2. **Redefine the key as a leg count.** A poor stopgap: leg counts drift on partial
   closes and per-leg assignment, and this same session observed a transient odd
   count (`broker_legs: 1`) during fill propagation.

Path 1 shipped — `RiskSnapshot.open_spread_equivalent`, `ceil(legs / 2)`, compared
against `max_concurrent_positions` in `gate.py` — **and `max_concurrent_positions`
moved 2 → 1 in the same edit**, so deployed behaviour is unchanged: one open
vertical, exactly what ran on 2026-08-13. The correctness fix and the exposure
increase are two decisions, and welding them together would have shipped the
second one unexamined. Settled details:

- **The count stays BROKER-derived.** Only the units change. A ledger-derived
  spread count was rejected outright: the gate reading the broker's number is what
  makes a position held-but-never-booked still consume a slot, so the system
  degrades toward not trading. → [unwatched-position gap](2026-08-11-unwatched-position-gap.md)
- **`AccountSnapshot.open_position_count` is untouched.** Its leg units are
  *correct* for `PositionWatchdog._check_broker_agreement`, which compares broker
  legs to ledger legs; the 2026-08-11 doc is explicit that `len(scan.spreads)`
  there would be wrong by 2x. The derivation lives on `RiskSnapshot` — the gate's
  own input type, and gate-only — rather than on the account model, so the
  book-agreement check cannot accidentally pick it up.
- **The gate stays a pure decision function.** `ceil(legs / 2)` is arithmetic over
  an already-fetched snapshot; nothing was added that fetches.
- **Odd leg counts round UP.** `broker_legs: 1` is a real observed state. Half a
  vertical is a position, not half a licence to open another; flooring would hand
  out a free slot at exactly the moment the book is least well understood.
- **`gate.decision` now carries both units** (`open_position_count` legs, kept
  under its original name so existing analysis still reads, plus
  `open_spread_equivalent`), and `/status` shows "2 legs (1 spread)".

**Why the cap first went to 1, and why it then went to 2.** The correctness fix
shipped at 1 so that it changed nothing: welding a doubling of exposure to a units
bug would have shipped the risk decision unexamined. Two spreads at
`max_loss_per_position = 1250` permits **$2,500 of defined risk**, and the breaker
only halts NEW entries — it cannot close what is already open — so against the then
`daily_max_loss` of $1,740 it would have tripped having already authorized more
risk than it exists to cap. Those two numbers had never been reconciled with each
other.

**Richard then took the decision to 2, signed off FOR CERT/SANDBOX ONLY
(2026-08-13), raising `daily_max_loss` 1740 → 2500 in the same edit.** The
reasoning is measurement validity rather than appetite: the shadow record was read
on the assumption of **two concurrent spreads**, so running a single vertical would
have made the live-fire results non-comparable with the analysis they exist to
test. A risk rail set below the thing being measured produces a clean number about
the wrong system.

The paired change also settles the reconciliation the old values lacked. **The
invariant is now explicit and exact:**

```
max_concurrent_positions × max_loss_per_position ≤ daily_max_loss
                       2 ×                 1250  =           2500
```

Nothing in `config.py` enforces this yet — it is held by the comments in
`argus.toml` and by the standing landmine in `CLAUDE.md`. Move any of the three
keys and re-check the other two.

**What sandbox still does not prove.** `daily_pnl` is structurally 0 in cert
(Finding 2), so the breaker does not actually enforce $2,500 here. The equality
above is arithmetic that this environment has never exercised — a day CAN run past
it in sandbox with nothing intervening. Both the breaker feed and this exposure
level need real validation before live, and the sign-off above does not extend
there.

`max_concurrent_positions` still must not be raised to 4 to "get two spreads" —
now doubly so, since the key finally means what it says. That is the standing
landmine recorded in `CLAUDE.md`.

## Finding 2 — the daily max-loss breaker is structurally inert in sandbox

Every `gate.decision` for the entire session carried exactly one
`(daily_pnl, account_nlv)` pair: **`(0.0, 100000.0)`**. Unchanged after a round
trip that realized a loss; unchanged with positions open.

The mechanism is not a flaky feed. `RiskSnapshotBuilder.build` **derives** day P&L
from NLV — `daily = self._day_pnl.value(nlv)` — and cert never marks NLV, so
`daily_pnl` is identically zero **by construction**. `gate.py` feeds exactly that
value to `breaker.evaluate`, which trips only on `daily_pnl <= -threshold`.

**The breaker therefore cannot trip in sandbox under any circumstances.** A
"daily-loss breaker: not tripped" reading on `/status` is not evidence the rail
works, and no amount of further sandbox running will make it one. The breaker's
own logic is unit-tested; what sandbox cannot exercise is the *feed*.

**This is a LIVE-FLIP GATE.** Before `environment = "live"`, prove a non-constant
`daily_pnl` reaches the breaker — either synthetically through the adapter, or by
confirming NLV actually moves on a live account.

**Sizing is unaffected, and this vindicates an existing rule.** The absolute rails
are deliberately never gated on a readable NLV, so a frozen NLV cannot produce an
oversized position; only the non-binding `position_size_pct` degrades.

## Finding 3 — `booked_but_absent` is routine post-fill behaviour

Two `watchdog.book_disagreement` records, both `booked_but_absent`
(`broker < ledger`), both resolving to `book_agrees` at 2 legs:

| Disagreement | broker vs ledger | Resolved after |
|---|---|---|
| 14:00:08.9 | 0 vs 2 | 5.01s |
| 14:22:09.5 | 1 vs 2 | 4.01s |

Both follow an **entry** fill, not an exit. Argus's ledger books the fill before
the broker's position book reflects it, sometimes leg-by-leg (`broker_legs: 1`).

This **inverts the concern the check was built for.** The unwatched-position gap
assumed the ledger could lag the broker; here it consistently leads. The
`held_but_unbooked` direction — the dangerous one — did not occur.

**Consequence worth acting on:** the check logs at `logger.error` and fires a
`watchdog_book_disagreement` Slack alert. Rising-edge, so once per episode — but
that is one ERROR page per entry for behaviour that now looks ordinary. Same shape
as [the 2026-08-05 alert storm](2026-08-05-slack-alert-storm.md): a real signal
made audible at the wrong threshold.

**FIXED** — `watchdog.book_settle_s`, a settle allowance on the
`booked_but_absent` direction **only**. Settled details:

- **The directions are now asymmetric, and that asymmetry is the whole fix.**
  `held_but_unbooked` means a real position may be held with nothing watching it,
  so it pages on the FIRST observation, unchanged. Only `booked_but_absent` waits.
- **TIME-based, never tick-based.** The check runs from `_refresh_if_due`, which
  fires on `refresh_interval_s` OR on any `request_refresh()` from the fill
  consumer — so the cadence is irregular by design and a tick count measures
  nothing. The window is wall time since Argus first *observed* the disagreement.
- **The allowance must not arm the escalation slot.** `_Escalation.rising` is what
  makes an episode page exactly once; marking it during the window would consume
  the edge and the page would never come at all. Suppressed observations write
  Tier 2 only and leave the slot un-armed — asserted directly by a test, because
  it is the one way this fix could have silently disabled the rail it is trimming.
- **A suppressed episode is still fully reconstructable**: every suppressed
  observation writes Tier 2 `watchdog.book_disagreement_still` carrying
  `settling_s`, and a resolution inside the window writes Tier 2
  `watchdog.book_settled` with `settled_after_s`. There is no Tier 1
  `book_agrees` for an episode that never paged — nothing to close.
- **20.0s in `argus.toml`**, ~4x the observed 4–5s and above `refresh_interval_s`
  so a full ledger refresh can elapse inside it even with no fill-driven refresh.
  **Re-tune it from `watchdog.book_settled` durations, not from a guess.**
- **Required config key, not defaulted, and `0` is valid** — zero restores the
  instant page with no code path of its own to rot. Required because every other
  `[watchdog]` key is, and because the operator, not this document, should pick
  the number; the cost is that config and image must deploy together (they must
  anyway — the droplet's `argus.toml` does not roll back with the image tag).

The residual risk is stated rather than designed away: a genuine
`booked_but_absent` — the ledger believing it holds something the broker does not
— now pages up to `book_settle_s` plus one refresh interval later than it did.
The response was always "a human looks", never an automated halt, so the delay is
small against the reaction time it feeds.

## Finding 4 — a stale leg quote blinded the underlying backstop

**The worst of the four, and the only one that left a live position genuinely
unwatched.** Repeated `watchdog_position_unevaluable` alerts on an open call
spread (7835/7840) carried `true_reasons: []` and `unknown_reasons:
[credit_multiple, underlying_near_short_strike]` — *both* triggers undetermined,
so for those ticks nothing was evaluating the position at all. The rate worsened
from ~1% of watchdog ticks early to **~14% by the end of the session**.

The mechanism, verified from source:

- `LegQuotes.resolve` finds the contract in the subscribed chain index FIRST, then
  looks up the quote.
- On a stale or missing quote it early-returned `LegResolution(problem=...)` with
  `leg=None` — **discarding the contract it had already found**.
- `evaluate_triggers` read the strike off `short.leg.contract`, so `leg=None` meant
  `short_contract=None`, and the underlying check reported "short strike unknown".

Leg quote ages were 2.0–2.9s against `market_data_max_age_s = 2.0`, because a
~55-point-OTM 0DTE call simply does not update more than every few seconds. So the
threshold was doing its job and the backstop was collateral damage.

**This contradicted invariant 3 of the watchdog's own module docstring** — "the
underlying trigger is evaluated even when leg quotes are not" — which held in
`evaluate_triggers` and was broken one layer beneath it. `contract.strike` and
`contract.right` are read from the chain index and have never depended on quote
freshness.

**FIXED — identity and price are now separate facts.** `LegResolution` carries
`identity` (the chain lookup) alongside `leg` (the priced quote), and exposes a
`contract` property that prefers `leg.contract` so the two cannot disagree. The
stale and no-quote branches keep the contract; only the truly-unknown-symbol
branch returns without one. `evaluate_triggers` reads `short.contract`, so a stale
wing now costs the CREDIT trigger and nothing else. Settled details:

- **`market_data_max_age_s` was NOT loosened, and must not be.** It governs entry
  pricing and the pre-trade gate's staleness rail too; widening it to rescue the
  backstop would quietly relax two unrelated fail-closed checks. The bug was the
  discarded contract, not the threshold.
- **The `.leg` call sites that need a PRICE are untouched** — the crossing-order
  repricer still refuses on `leg is None`, correctly.
- **The page was re-levelled in the same change.** `TriggerEvaluation.blind` (every
  trigger unknown) is what fires `watchdog.unevaluable` + the Slack alert now.
  One trigger dark with the other live is `watchdog.degraded`: Tier 2, rising edge,
  `logger.warning`, **no page**. At 14% of ticks the old behaviour was one alert per
  ~15 minutes with 100+ suppressed behind it — the 2026-08-05 shape again, and it
  buries the blind case that does matter. `blind` is derived from
  `frozenset(TriggerReason)` rather than written out, so adding a third trigger
  cannot silently retire the page.
- **Recovery from blind → degraded still writes `watchdog.evaluable_again`.** The
  Tier 1 page needs a counterpart even when the position is not fully clean;
  `unknown_reasons` in the payload says what is still missing.

**`/status` gained a live exit-triggers row in the same change**, because the audit
trail structurally cannot answer this on a phone. `watchdog.check` carries
`unknown_reasons` every tick, but audit payloads serialize with **sorted keys**,
which puts `unknown_reasons` LAST — after the long `problems` strings — and
`/status`'s HTML tail truncates a payload at 240 characters. Measured on a
representative degraded record: 397 chars, with `unknown_reasons` falling 157 chars
past the cut. It is not a near miss; it is never visible, and the longer the problem
strings the further out it goes. JSON `/status` was always fine; the phone was
structurally blind to it, and the phone is the surface that matters for an
unattended system.

So `PositionWatchdog.impaired_positions()` is a live read, and `session.py`
publishes it through the same seam `LinkHealth` uses. Settled details:

- **Sourced from the SAME `TriggerEvaluation`** that produces the audit record —
  `_note_evaluability` is called from `_report_evaluability`, before the audit
  branches. One computation, two consumers, so the phone view and the trail cannot
  drift and disagree later. A test asserts they report identical reasons.
- **`impaired_for_s` is the point, not a nicety.** "Degraded right now" and
  "degraded for 20 minutes and still not clearing" are different operational
  signals and only the second says go and look. The clock restarts when the reason
  SET changes, so a position going degraded → blind reports the blind episode's
  age rather than inheriting the milder one's — duration and severity always
  describe the same state.
- **THREE render states, deliberately distinct.** `None` = no watchdog publishing
  (shadow, or no running session): the question does not apply. `()` = live and
  everything evaluable. Non-empty = impaired, per position, with `blind` styled as
  the emergency and `degraded` not. Collapsing None into "all evaluable" would tell
  an operator every position is fine when nothing is being watched at all.
- **Entries are dropped when a position stops being evaluated** — closed out of the
  ledger, or moved to `_exiting`/`_attempted`. A frozen reading would be a lie that
  only gets older, which is precisely the field's whole value.

Note what the fix does NOT buy: with both legs stale, a fired backstop still cannot
BUILD an exit order (`watchdog.exit_unpriceable`), so the trigger latches, pages,
and retries each tick until a leg prices. That is the designed behaviour and is
strictly better than the trigger never firing — but "the backstop works with stale
legs" means it *fires*, not that it *exits*.

## Finding 5 — the Tier 1 evaluability flap, and what is owed on it

**1,517 Tier 1 `watchdog.unevaluable` records plus 2,854 Tier 2
`watchdog.unevaluable_still`, across 15,711 checks** — 27.8% of ticks unevaluable,
entering and leaving the state ~1,517 times. `_Escalation` is a pure
rising/falling edge with no hysteresis, so a *flapping* condition defeats it
entirely: every flap re-arms the slot and pages again, and every one is a Tier 1
write — awaited, holding the writer lock, and fsync'd since `synchronous = FULL`.

**Finding 4's fix should have largely removed this, and "should have" is not a
measurement.** `_report_evaluability` now levels `blind` (no trigger evaluable) at
Tier 1 + page and `degraded` (one trigger live) at Tier 2 only, and the stale-wing
case that produced nearly all of 2026-08-13's volume is *degraded*, not *blind*.
So the expected post-fix count is small. Expected is not measured.

**BLOCKED ON DATA, deliberately, and this is the whole finding.** Fix C is a
`src/` change and 2026-08-13's session ran the code before it, so **no session has
yet run under Fix C** — the first one is the measurement. Sizing a settle
allowance from the pre-fix numbers would size it for a condition the fix was
supposed to have removed, which is exactly the guess `book_settle_s`'s own
"re-tune from the records, not from a guess" rule exists to forbid.

**What shipped instead is the instrument.** `argus.tools.evaluability` — read-only
(`mode=ro`, no lock, safe mid-session), one ET trading date, one command:

```
./.venv/bin/python -m argus.tools.evaluability --config argus.toml
```

It prints the category counts by TIER, reconstructs distinct blind episodes from
the Tier 1 rising/falling pair keyed on `external_identifier` (the same key
`_Escalation` itself uses, so "distinct episode" means the same thing in both),
reports each episode's duration and the GAPS between episodes on one position,
and prints the audit write rate split by tier. Settled details:

- **Episodes, not tick counts.** "27.8% of ticks" is a symptom; the decision turns
  on how many times the state was ENTERED and for how long, because that is what
  a settle allowance suppresses. A single 4-hour blind episode and 1,517 one-second
  ones give a similar tick percentage and call for opposite responses.
- **The gaps are reported because they are the flap signature.** An allowance only
  helps when a position recovers and immediately re-enters; one going blind once
  an hour is not flapping, it is failing repeatedly, and suppressing that would be
  hiding a real fault.
- **An episode still open at the last record is measured to THERE and flagged**,
  never treated as zero-length — a position that went blind and never recovered is
  the worst case, not a missing data point.
- **It prints the decision RULE and does not apply it.** A threshold for "this
  flaps" living in the tool would quietly become the decision. The tool measures;
  a human picks the branch.
- **A day that held no position reports INCONCLUSIVE, never DONE.** The watchdog
  evaluates OPEN positions only, so a session that never filled writes no
  `watchdog.check` at all — and zero blind episodes there is not evidence the flap
  is gone, it is evidence there was nothing to be blind about. Without this guard
  the cheapest possible way to close this finding is to run the tool on a no-trade
  day. **So the measurement is the first session that actually HOLDS a position,
  not merely the first session under the new image.**
- **Percentiles are nearest-rank, never interpolated**, so every duration printed
  is an episode that actually happened — these are what an allowance gets sized
  from.

**The decision, when the data exists:**

- Rare and non-flapping → **DONE, no code change.** Record the measurement here.
- Still flapping → a settle allowance before `blind` escalates, **mirroring
  `watchdog.book_settle_s` exactly**: a new `[watchdog]` key, TIME-based on wall
  time since the condition was first observed, suppressed observations still
  writing a Tier 2 trail, `0` disabling it. **It must NOT reuse `book_settle_s`** —
  different condition, different urgency, and one key governing both would tie the
  page for "nothing is watching this position" to the page for "the broker's book
  has not caught up yet". Size it from the measured episode durations.

### RESOLVED 2026-08-18 — the flap is gone. DONE, no code change.

The first branch. Measured on the session of 2026-08-18, which held two positions
(a call and a put spread, both to expiry) across 43,104 watched ticks over 6.33h:

| | 2026-08-13 (pre-Fix-C) | 2026-08-18 (post) |
|---|---|---|
| Ticks blind | **27.8%** (of 15,711) | **0.0%** (0 of 43,104) |
| Tier 1 `watchdog.unevaluable` | 1,517 | **0** |
| Tier 2 `watchdog.unevaluable_still` | 2,854 | **0** |
| Distinct blind episodes | ~1,517 | **0** |
| Tier 2 `watchdog.degraded` | — | 2,954 |

Fix C did not reduce the flap, it eliminated it. The 2,954 `degraded` records are
the mechanism working exactly as designed: the stale-wing case that produced
nearly all of 2026-08-13's Tier 1 volume is *degraded*, not *blind*, and now
levels at Tier 2 where it belongs. **No settle allowance is owed**, and the
`[watchdog]` key sketched above is deliberately NOT added — there is no condition
left for it to suppress, and adding it "for completeness" would be a live
behaviour change with no measurement behind it.

**Why this is the first valid measurement, and not an earlier one.** Fix C shipped
in v0.3.1 on the evening of 2026-08-13. 2026-08-14 was lost entirely to the
reconciliation halt (no session). 2026-08-17 held three spreads but two of them
were **unwatchable** — the overlapping-verticals defect meant they produced no
`watchdog.check` records at all, so the tool would have reported zero blind
episodes while measuring only one of three positions. That is precisely the shape
the "a day that held no position reports INCONCLUSIVE" guard exists to catch, one
level up: not "nothing was held" but "not everything held was watched". 2026-08-18
is the first session where every position taken was also watched.

**Write rate, the second question the tool answers**: 4,651 Tier 1 (0.20/s) and
48,411 Tier 2 (2.12/s), 2.33/s total. Total volume ROSE against 2026-08-13's
~1.7/s while the fsync cost FELL, because the flap moved from Tier 1 to Tier 2.
`argus.toml`'s `[audit] synchronous` comment now cites both numbers and says which
one pays. At 0.20 Tier 1 records/sec, FULL costs ~16 microseconds per second of
runtime.

## Finding 6 — a live-fire day produced no scoreable P&L record

**The signal-quality series stopped the moment orders started filling for real** —
the same failure as the 2026-08-11 sandbox flip, one environment later.
`shadow/marker.py` scored from `("shadow.would_submit", "entry.dry_run")`, and the
live entry path writes neither. Its terminal record is `entry.finished`, which
carried the walk report and no strikes, credit or size.

**But a third category alone would have been wrong**, and that is the more
interesting half. Settlement scoring assumes the idea was never filled and rode to
expiry — true by construction in shadow and dry-run, because nothing is ever held.
A live position the watchdog closes intraday has a REALIZED result the 16:00 print
says nothing about. On 2026-08-13 the 7825/7830 call spread opened at 0.55 credit
and closed at 1.20 debit at 10:21 ET: **-$130 on 2 contracts, against a settlement
that finished the spread full credit.** Scoring it at settlement would have
reported a win on a trade that lost money.

**FIXED — one formula with two terms, and every mode is a corner of it:**

```
P&L = realized on the contracts CLOSED  +  settlement on the contracts HELD
```

An idea that never filled has no execution, both counts are zero, and it scores
its PROPOSED size at settlement — the pre-existing behaviour, unchanged, for every
record ever written before live fire. A position closed in full has no second
term. One held to expiry has no first term. **A partial close has both and needs
no special case to get there**, which is why it is a sum rather than a branch.
Settled details:

- **Fill prices, never order limit prices.** The exit filled at 1.20 net against a
  -1.30 limit; the limit would have overstated the loss by $20 on 2 contracts.
- **`entry.finished` gained `markable_payload`**, additively, exactly as
  `entry.dry_run` did on 2026-08-12 — same function, three writers now, and
  `test_the_marking_block_is_byte_identical_across_all_three_writers` is what
  stops them drifting. It is written for unfilled outcomes too (walked away,
  drifted, rejected) and that is deliberate: a declined idea is still a proposal
  at a credit, scoreable against settlement exactly like a shadow one.
- **The block goes into the RECORD, not the alerts.** `_report` hands the same
  dict to `_alert` for several outcomes; those are read on a phone and gain
  nothing from the strikes appearing in a second nesting.
- **Fills are attributed through `external_identifier`, NOT through the `orders`
  table.** `candidate_id` is NULL on every `order.fill` — it does not round-trip
  through the broker — but the identifier does, and it is
  `argus-{direction}-{candidate}` by construction, so it names both the candidate
  and which half of the round trip a leg belongs to. That keeps scoring entirely
  inside the append-only log: `orders` is mutable current-state, upserted in
  place, and scoring a date months later has to read what was RECORDED then. The
  parsers (`parse_external_identifier`, `parse_candidate_key`) live next to the
  constructors in `execution/build.py` so a format change is a change to a
  neighbour, with a round-trip test.
- **The cash sign comes from the leg ACTION, the direction from the identifier,
  and a record where they disagree is dropped.** A closing BUY and an opening BUY
  both pay premium, so anything deriving cash flow from open-vs-close prices an
  exit backwards. `LegAction.sells` is now the single definition —
  `store.signed_leg_delta` reads it for the position sign and scoring reads it for
  the cash sign.
- **An unpriced fill is `unpriceable`, not zero.** `LegFill` is documented as
  best-effort: it prices a fill, it does not count one. With no price detail the
  P&L is `None`, the row prints `-`, it is excluded from the total, and the tool
  says so loudly. A fabricated 0.00 in a summed column is worse than a hole.
- **The counterfactual is recorded alongside the score.** `counterfactual_pnl` is
  what the position would have been worth held to settlement at its actual fill
  credit; the difference between it and `pnl` is what the virtual stop cost or
  saved on that trade, which is the number `credit_multiple` will be tuned on.
- **`breached` still describes the 16:00 print, not the trade** — a position the
  stop closed at a loss can show `breached = false`, and that is precisely the
  observation that says the stop fired on a move that came back.
- **ONE operator command, no mode flag.** `--settlement`/`--from-feed` stays
  required even on a day where everything closed intraday; the settlement term is
  multiplied by zero held contracts and the number is simply unused. Cheaper than
  a second code path, and much cheaper than an operator having to know in advance
  which kind of day it was.

**Environment-independence is preserved exactly as the 2026-08-12 doc requires.**
The second reader is keyed the same way as the first: a day with no `order.fill`
records scores as it always did, whether it had none because it was a shadow day,
a dry-run day, or a live day where every walk walked away. `marker.py` still
imports no `Config` and names no environment, and the AST test still enforces it.

### 2026-08-13 is NOT backfilled — because it does not need to be

That day's live entries wrote `entry.finished` before it carried a markable block,
so there is no proposal record to decode. **There are still fills**, and
`candidate_id` IS `(date, side, short, long)` by construction — so `_synthesize`
reconstructs the idea from the fills alone and the position scores through the
normal path, with no second decoder and no one-off script.

**This is a rail, not a backfill helper, and the distinction is the reason it is
in the tree at all.** The 2026-08-12 doc declined its own backfill on the grounds
that write-once recovery code would sit in the tree misleading the next reader;
that judgement stands and this is not a counter-example. A missing proposal record
costs one counterfactual data point. A missing FILLED position costs real P&L and
leaves the account's own history disagreeing with the signal record — so "a
candidate with fills is always scored" is a permanent invariant worth code,
whatever wrote (or failed to write) the proposal.

What IS lost for 2026-08-13, and accepted: the re-proposal history for those
ideas (reported honestly as `proposals = 0`), and the counterfactual score for any
`entry.finished` that did not fill. The second is the same class of gap the
2026-08-11 dry-run days carry, accepted for the same reason — and on 2026-08-13 it
is small anyway, since the leg-scoped concurrent limit (Finding 1) denied 402 of
404 gate decisions, so very little reached the entry path at all.

## Also confirmed

- **The warmup stall is exactly as measured.** Loop lag max 505.2ms, 6 breaches,
  1 sustained — reaching `sustained_breach_count` at the open, as on 2026-08-05.
  `trip_on_sustained_stall = false` is the only reason the day did not halt at
  09:40.
- **No staleness or size rail denied anything.** Zero `market_data_stale`, zero
  `account_stream_stale`, zero `position_max_loss_exceeded`, zero
  `contracts_exceeded`, zero `invalid_proposal` across 404 decisions.
- **Fill → `positions` → watchable is fast enough**, answering the "also worth
  confirming" note in the open-questions section — with the caveat in Finding 3
  that the ledger led rather than lagged.
- **Memory grew ~70MB/h** (177.5MB RSS, +74.4MB since start) with gen-2 at 1.9/h.
  The GC mitigation holds; the underlying live-object accumulation behind
  [the lag ramp](2026-08-10-intra-session-lag-ramp.md) is still unidentified.
