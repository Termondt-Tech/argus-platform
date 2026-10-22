# An empty broker answer is not "terminal, safe to cross" (2026-08-21)

*`any()` over an empty list is `False`. That one line of Python semantics let an
order the broker never mentioned satisfy "nothing is working" and authorize a
SECOND closing order on a spread whose first close was still at `Cancel Requested`
— the exact double-close `_cross`'s own docstring names as the thing it exists to
prevent.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

Finding A3 of the 2026-08-20 exit-path pass. Found by reading, not by an incident.

## The defect

`ExitExecutor._await_settled` (`watchdog.py:1031`) is the gate on the market-crossing
escalation. The walk gave up, `cancel()` has been requested, and this decides whether
the abandoned order is dead enough that a second closing order is safe. It asked the
broker, then:

```python
dispositions = [classify_order(o) for o in found]
if any(d is OrderDisposition.FILLED for d in dispositions):
    return ExitOutcome.FILLED_LATE
if not any(d is OrderDisposition.WORKING for d in dispositions):
    return None                      # -> the cross proceeds
```

With `found == []` — an answer that does not mention the order at all — both `any()`
calls are `False`, so an order nobody read reached the same conclusion as an order
the broker positively confirmed cancelled. `None` is the one return value that
authorizes `_cross` to submit.

`_cross`'s docstring already names the consequence, which is why this is a defect
and not a design question:

> Submitting a second closing order on top of one that can still fill closes the
> spread TWICE, which does not flatten the position: it re-opens it, inverted,
> unmanaged, on expiry day.

## Two frames away, the same signal is read the opposite way

`PriceWalkingLoop._walk` (`loop.py:708`) meets an identical absence every cycle and
does not conclude anything from it:

> The order was not in the broker's answer. That is a read gap, not a verdict, and
> repricing an order we cannot see would be acting on an assumption.

It counts `blind_cycles` and gives up only after `max_recovery_attempts` consecutive
blind answers. The walk applies that discipline where the consequence is *repricing*;
`_await_settled` did not, where the consequence is *a second order*. The stricter of
the two call sites had the weaker rule.

## How an order goes missing

`LiveOrderView.by_identifier` filters `get_live_orders()`, so anything the adapter
cannot represent, or the broker does not report, is absent rather than named. Since
[C2](2026-08-20-unmappable-orders-reach-reconciliation.md) the *unmappable* route is
covered — an unreadable order under the queried identifier RAISES, which
`_await_settled` already retries and then refuses on. C2 shrinks how often this path
is reached; it does not change what happens once an answer is genuinely empty, which
is why A3 stayed a separate change. A read gap with no unmappable row to explain it —
a truncated or empty `/orders/live` payload, a reporting-window boundary — still
produces `found == []`.

## The fix

**Only a positive confirmation confirms.** The broker must NAME the order and every
disposition it gives must be terminal. Everything else — absent, still working,
unreadable — is retried inside the same `watchdog.exit_cancel_settle_s` window and
refuses at the deadline with `CROSS_REFUSED`. Absence is now handled exactly the way
an exception already was: the deadline is the bound, not the first look.

That is a two-line change (`if not dispositions` before the `any(WORKING)` test), and
the rest of the diff is about saying which one happened.

**The page must not describe one refusal as another.** `_await_settled` returned a
bare `ExitOutcome | None`, so `_cross` composed the operator's sentence from the
outcome alone and could only guess — it said *"abandoned exit order was still working
after 3s"* for every refusal, including the unreachable-broker case that was already
reachable before this change. It now returns `_Settlement` (`watchdog.py:670`),
carrying the outcome and the reason together, keyed on the last thing the broker
actually said:

| Last readable answer | The refusal says | Where the operator looks |
|---|---|---|
| `working` | still working after the window | there IS a live order that can fill |
| `absent` | absent from the answer, never confirmed terminal | the read — and the broker, for an order nobody can see |
| `unreadable` | could not be read in the window | the connection |

All three refuse. `CROSS_REFUSED` is unchanged as an outcome; what changed is which
observations can reach it.

## What CROSS_REFUSED does downstream (traced, not assumed)

1. `_cross` returns **before** `self._execution.submit` — no second closing order,
   and no `watchdog.crossed` record.
2. `_notify("watchdog_cross_refused", …)` — WARNING log + `AlertFn` → phone.
3. `close()` → `_record_exit` → Tier 1 `watchdog.exit_finished` **and** a second page,
   `watchdog_exit_cross_refused`.
4. `_run_exit` → `_attempted.add(key)`. **The position is not retried**, deliberately:
   a second exit attempt is the double-close this whole path exists to prevent.
5. `check_once` skips the key from then on.

So it is loud — two pages and a durable record — and terminal. Refusing leaves a
losing position open and pages a human, which is the trade `_cross` already made
explicitly for the still-working case and which absence does not change.

**One thing this does NOT do, recorded rather than fixed here:** the pages are
one-shot. There is no repeating silence bound of the kind
[`exit_unattempted_max_s`](2026-08-20-exit-order-freshness-bound.md) gives
`_UNATTEMPTED`/`_UNPRICEABLE`, and the position has been dropped from evaluation, so
a missed notification restores exactly the silence that decision was written against.
It is a different case — there IS an order in the market that may still fill, and
re-attempting is forbidden — so the answer is not a copy of that one. See the open
item in [`README.md`](README.md#open-decisions).

## Coverage

`tests/test_watchdog_exit.py`, over the real `ExitExecutor` / `PriceWalkingLoop` /
`AuditedOrderExecution` stack. `VanishingBroker` cancels to `Cancel Requested` — the
order is still alive and can still fill — and from that moment reports nothing under
the identifier. The whole identifier, not the one order: `get_live_orders()` returns
today's orders *including terminal ones*, so the walk's replaced-and-cancelled rungs
are in the answer too, and an empty answer is a read that lost the trade idea.

Two mutations, both caught:

| Mutation | Caught by |
|---|---|
| Drop the `if not dispositions` guard (the original defect) | `test_an_empty_broker_answer_refuses_the_cross_instead_of_reading_as_dead` — reproduces the double-close: `CROSSED`, with a second broker order id on top of the one still at `Cancel Requested` |
| Treat every answer as uncertain (never confirm) | `test_a_terminal_answer_after_a_read_gap_still_settles_and_crosses` + 4 existing cross tests |

The second mutation is the one worth keeping: the pair bounds both sides, and the
"still crosses" test alone would have passed against the bug.

## Residual, found while writing the test — CLOSED as A3b

A **partial** answer was not covered by this fix and is not covered by C2 either. If
the broker's answer names the walk's older replaced-and-cancelled rungs but omits the
final working one, `dispositions` is non-empty and every disposition in it is
terminal — a confirmation by this rule, and the cross proceeds against an order still
at `Cancel Requested`. C2 closes the mapping route to that (an unmappable order under
the identifier raises), leaving a broker-omission route open.

Closing it needs the confirmation to be scoped to a specific broker order rather than
to the identifier. The data is already there — `WalkResult.broker_order_id` is the
last id in the replace chain, i.e. exactly the abandoned order — so this is a small
change, but it narrows what "settled" means on a live-flip-path safety rail and was
not in A3's scope. Recorded as an open item rather than folded in.

**Closed 2026-08-21** by
[A3b](2026-08-21-partial-broker-answer-is-not-a-verdict.md), which applies the rule
above at that granularity: absence is ONE case whether the answer is empty or merely
omits this order, so the paragraph on this page describing coverage as "the whole
identifier, not the one order" now describes only what the *test broker* hides, not
what the check reads. The FILLED test remains identifier-wide there, deliberately.
