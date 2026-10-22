# The exit path was gated by an entry-path invariant (2026-08-20)

*`execution.signal_max_age_s` exists to keep a submitted price equal to the price
the gate approved. Nothing gates an exit — but exit construction was enforcing that
bound anyway, so the 1.0–2.0s band produced triggers that could never become
orders. Fixed by keying exit construction off the trigger's own freshness bound,
and by bounding how long a triggered position may sit with no order and no page.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

## What was observed

The first intraday stop since v0.3.5. The credit trigger fired and sustained; the
exit was not attempted:

```
signal is 1.313s old, past the 1s construction tolerance;
refusing to build an order from a price the gate did not see
```

That is `StaleSignalError` out of `build_order`, surfaced as
`ExitOutcome.NOT_ATTEMPTED` and escalated once as `watchdog.exit_unattempted`.

## Mechanism

Two thresholds, each correct in its own place, wired to the wrong path.

| Key | Value | What it bounds |
|---|---|---|
| `execution.signal_max_age_s` | 1.0 | Gate-pass → order construction, on the ENTRY path |
| `staleness.market_data_max_age_s` | 2.0 | Quote age the feed is trusted at, everywhere |

`signal_max_age_s` protects one invariant, stated in `build.py`: *the price
submitted must be the price that was sized and passed the pre-trade gate.* It is
deliberately tight because the window it bounds is short — the handful of
milliseconds between the gate returning and the order being built. The loader even
refuses a value above `market_data_max_age_s`, on the reasoning that the gate's
looser rail would bind first.

**That invariant does not exist on the exit path.** Nothing gates an exit. The
watchdog decides, on its own, against `market_data_max_age_s` — the bound
`LegQuotes.resolve` already applied to both legs before a `SpreadMarket` exists at
all. `ExitExecutor._build_exit_order` nevertheless passed the entry value into the
shared builder:

```python
max_signal_age_s=self._config.execution.signal_max_age_s,   # 1.0
```

So the two ends of the same exit disagreed by a factor of two. A quote between 1.0s
and 2.0s old is fresh enough to *fire* a stop and too stale to *act* on one — and a
0DTE wing tens of points OTM updates every few seconds, so that band is ordinary,
not exceptional. The same module docstring already says this in a neighbouring
context: *"a 0DTE wing … breaches a 2s bound routinely — that is the quote feed
being honest, not a misconfiguration."*

## The fix

Exit construction keys off `staleness.market_data_max_age_s`.

A bound is kept rather than removed. A quote past the trigger's own threshold is
one the trigger itself would refuse to evaluate, so there is nothing honest to
price from; what is gone is the *second, tighter* rule that no exit-path invariant
ever asked for. `signal_max_age_s` is untouched and stays entry-only.

**Explicitly rejected: raising `signal_max_age_s`, or introducing a third value.**
Raising it would relax the entry invariant to fix an exit bug. A new key would be
a third number meaning "how fresh must a quote be", which is the drift
`book_settle_s`'s one-knob-for-two-windows note already warns about. This is the
same rule as [Finding 4 of the live-fire pass](2026-08-13-live-fire-sandbox-pass.md):
never fix an exit-path staleness problem by widening a threshold that also governs
entry pricing and the pre-trade gate.

## The second half: the retry was bounded, the silence was not

The original report left one question open — whether a failed exit construction is
bounded and escalates. **It was not, and it escalated exactly once.** Read as
built:

- `close()` returns `NOT_ATTEMPTED` without recording or alerting, deliberately:
  nothing was sent, so the position keeps its one attempt.
- `_run_exit` escalates on the RISING edge — one Tier 1 `watchdog.exit_unattempted`
  and one page — then every subsequent tick is Tier 2
  `watchdog.exit_unattempted_still`.
- Nothing ever fires again. The retry loop spins at `cadence_s` for as long as the
  condition holds, and after the first page the operator hears nothing more.

`watchdog.exit_unpriceable` — the legs cannot be priced at all — has the identical
shape, and is bounded here too. It is the same class of state: *the watchdog has
decided this position must be closed, and no order exists.*

**The retry itself stays unbounded, and that is correct.** Abandoning would strand
an open 0DTE position the watchdog has already decided must be closed; consuming
the position's one attempt on an order that was never sent would be worse. What
needed a bound was the silence.

So `_Escalation` gained `overdue()`. Every `watchdog.exit_unattempted_max_s` (30.0s
= 30 cadence ticks) the condition survives, one further Tier 1
`watchdog.exit_still_unplaced` and one further page fire, naming the condition and
how long it has held.

### It repeats, and the first version was wrong to fire once

Worth recording because the mistake is an easy one to make again. The first
implementation fired **once per episode**, re-arming only on the falling edge. That
was not reasoned to — it fell out of reusing `_Escalation`, whose existing tags are
all rising-edge, and the "must not become the 2026-08-05 alert storm" justification
was written afterwards to fit.

Two things break that justification:

1. **The storm lesson argues for a bounded RATE, not a single event.** Those are
   different rules, and conflating them is what produced the once-only version. The
   2026-08-05 storm was one alert *per sub-second tick*; one alert per 30s interval
   is not that.
2. **The rising-edge precedents are not analogous.** `_DEGRADED`, the closest one,
   is explicitly a condition that is *recorded and never paged*. It is the wrong
   model for the most severe state this module can be in.

And the cost is real: a single page is one missed notification — phone on silent, a
meeting, a swallowed push — away from restoring exactly the silence this exists to
prevent, for as long as the retry keeps failing. For "an open 0DTE position cannot be
closed", that is the wrong side to err on.

**The rate is bounded twice, neither time by suppressing the repeat:**

- `overdue()` counts whole multiples of the bound, so it fires per INTERVAL, never
  per cadence tick — ~1 Tier 1 write per 30s, so the writer lock spec §6 protects is
  never contended, and the trail stays readable.
- `SlackNotifier` already throttles at 900s and folds the rest into a
  `(+N suppressed)` count on the next delivery. **That is where phone volume is
  handled, one layer down** — the once-only version was re-implementing that defence
  here, more strictly, in a module that should not have known about it. In practice
  the phone sees roughly one message per 15 minutes, carrying the count that
  distinguishes "it happened again" from "it has been happening continuously".

  **Amended 2026-08-21 (B1).** That throttle keyed on `kind` ALONE when this was
  written, and the sentence above was a rate argument about one condition on one
  position. It does not transfer: `overdue` is per (position, condition), so two
  positions crossing their own bounds in the same fifteen minutes both fire
  `watchdog_exit_still_unplaced` — one kind — and the second was folded into the
  first's suppressed count. The throttle is now per (kind, position), and this
  escalation's payloads carry `candidate_id` at the top level so it can be. See
  [the alert throttle](2026-08-21-alert-throttle-is-per-position.md); the reasoning
  above holds unchanged at the new grain.

`unresolved_for_s` grows across repeats, so the audit trail reads as a duration
rather than a repeat count.

**Whole multiples, not "seconds since the last page".** The counter is
`int(elapsed // after_s)` against the episode's own rising edge. The obvious
alternative — remember when the last page fired, wait `after_s` from there —
accumulates float drift without bound, so a 30s alarm slowly becomes a 31s, 33s,
36s one over a long episode. Measured on the form actually shipped: with 0.1s ticks
against a 20s bound, a page lands at most ONE tick late, the lateness does not
accumulate (0, 1, 1, 1 — not 0, 1, 2, 3), and no interval is ever dropped. Late,
never early, is the safe direction for an alarm. A long gap between evaluations
collapses to one page carrying the true elapsed rather than a backlog of stale
ones, which is the useful thing to wake someone with.

**The counter is scoped to an EPISODE, keyed `(position, condition)`.** `falling`
and `forget` both clear it, so a re-entered trade idea, a recovered position, and
the OTHER condition on the same position each page at their own first 1x rather
than inheriting a spent count. That is the failure mode a count-only test cannot
see, so the tests assert the reported DURATION restarts too.

**Not a halt.** `_halt_process` sets the stop event, which ends the watchdog's own
loop — halting here would stop the retries that are the only thing still trying to
close the position. The escalation a human can act on is a page, not a shutdown.

**No "off" switch.** Unlike `book_settle_s`, where `0` restores an older and
stricter behaviour, `0` here would page on the tick after the rising edge — two
pages for one condition. The loader refuses it. An exit that can never be built is
never a condition to stop reporting.

## What this does not change

- `execution.signal_max_age_s` and its loader constraint
  (`<= staleness.market_data_max_age_s`): entry-path only, unchanged.
- One attempt per position. An order that reached the broker is still never
  retried.
- The exit walk, the crossing escalation, and every trigger threshold.

## Tests

`tests/test_watchdog_exit.py` pins the 1.313s case **building and filling** an
order (the fix), and 2.001s still refusing (the bound that was kept).

`tests/test_watchdog.py` pins the escalation on both no-order paths: it fires per
interval rather than per tick, **keeps paging across ten minutes of an unclosed
position** with a monotonically growing `unresolved_for_s` (the regression the
once-only version would have had), re-arms after the condition clears, and never
halts.

`_Escalation.overdue` is also pinned directly, on the primitive rather than through
watchdog behaviour, for the two things a multiple counter gets silently wrong:
**the boundary** (19.999 no / exactly 20.0 yes / 20.001 not twice, then the same at
40.0 — the same bracketing discipline as the freshness gate's 1.313 / 2.001) and
**the scope** (restart from 1x on a new episode, no leakage across positions or
across conditions on one position, `forget` clearing the count, and bounded drift
under accumulated float ticks). All verified non-vacuous by mutation: breaking the
reset fails three tests, and swapping the whole-multiple counter for the
"seconds since last page" form fails the drift test.

`tests/test_config.py` pins the key as required and `0` as refused.
