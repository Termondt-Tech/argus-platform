# A partial broker answer is not "terminal, safe to cross" either (2026-08-21)

*A price-walk exit leaves several orders under one `external_identifier` — the
abandoned rungs and the current one. An answer that names only the abandoned rungs
is non-empty and entirely terminal, which is exactly what A3's fix accepts as a
confirmation. Same double-close, one step sideways.*

> Settled decision — do not relitigate. See [`docs/decisions/README.md`](README.md)
> for the index.

Finding A3b of the 2026-08-20 exit-path pass, recorded as the residual of
[A3](2026-08-21-empty-broker-answer-is-not-a-verdict.md) and carried as an open
decision until now. Found while writing A3's test; closed by reading, not by an
incident.

## The defect

`get_live_orders()` returns **all of today's orders, including terminal ones**. A
price-walk exit sends multiple rungs over its lifetime, each replacing the last
(measured, chunk 1: a replace mints a NEW id, terminates the old one, and both keep
the same `external_identifier`). So `by_identifier` legitimately returning several
orders for one exit is the NORMAL case, not an anomaly — the abandoned,
genuinely-terminal earlier rungs plus whatever the current rung is.

A3 left the confirmation scoped to that whole list:

```python
dispositions = [classify_order(o) for o in found]
if not dispositions:
    last = _SETTLE_ABSENT                       # A3's fix: empty is a read gap
elif any(d is OrderDisposition.WORKING for d in dispositions):
    last = _SETTLE_WORKING
else:
    return _Settlement(None)                    # -> the cross proceeds
```

If the broker's answer names the earlier rungs and omits the current one — still at
`Cancel Requested`, still able to fill — then `dispositions` is non-empty (so A3's
guard does not fire) and every entry in it is terminal (so `any(WORKING)` is
`False`). The `else` branch is reached and `_cross` submits a second closing order.

The consequence is the one `_cross`'s own docstring names:

> Submitting a second closing order on top of one that can still fill closes the
> spread TWICE, which does not flatten the position: it re-opens it, inverted,
> unmanaged, on expiry day.

**This route needs no unmappable status at all.** [C2](2026-08-20-unmappable-orders-reach-reconciliation.md)
closes the mapping route — an unreadable order under the queried identifier RAISES,
which `_await_settled` retries and then refuses on. A3 closes the fully-empty route.
This one needs only an incomplete-but-non-empty answer, which nothing so far bounds.

## The fix

**A3's exact rule, at the granularity the question actually has.** The order in
question is not "the identifier"; it is the specific rung the walk abandoned, and
`WalkResult.broker_order_id` already holds it (the last id in the replace chain).
`_cross` now passes it, and `_await_settled` reads the answer for that id alone:

| `broker_order_id` in the answer | Verdict |
|---|---|
| present, terminal | confirmed — the cross may proceed |
| present, WORKING | not settled — retry inside the window, refuse at the deadline |
| **absent** — whether the list is empty or names only other orders | `CROSS_REFUSED` — a read gap, not a verdict either way |

The third row is deliberately one row and not two. A3's case (empty) and A3b's case
(partial) are the same case: the broker did not tell us about this order. Treating
the partial answer as a second, bespoke rule would leave two things to keep in
agreement where one rule covers both.

A fourth refusal reason joins A3's three: a walk that reports no
`broker_order_id` at all has nothing to confirm, and no amount of polling will
produce one, so it refuses immediately rather than spending the settle window on a
question that cannot be asked. Reachable only through `_recover` adopting a working
order the broker reported without an id — narrow, but `WalkResult.broker_order_id`
is `int | None` and the fail-closed reading of `None` is not "safe to cross".

### The FILLED check stays WIDE, and the asymmetry is the point

`_await_settled` narrows the *terminal* check and leaves the *FILLED* check across
the whole identifier. That is not an oversight, and the code says so where someone
would otherwise tidy it into symmetry:

- **Wide FILLED only ever refuses more.** A fill on ANY rung of this exit means a
  closing order filled and the spread is already flat.
- **Narrow FILLED would open a new hole.** With both checks scoped to
  `broker_order_id`, an answer holding an earlier rung's FILL alongside a cancelled
  current rung would read as "confirmed terminal, safe to cross" — the fill sitting
  right there in the same payload, ignored.

`external_identifier` carries the DIRECTION (`argus-close-…` vs `argus-open-…`), so
the wide check cannot be tripped by the entry order that opened the position.

## What was checked before narrowing, and left alone

**`by_identifier` itself is unchanged.** The fix narrows what `_await_settled`
treats as authoritative, not the query. Its other caller needs the wide match and
must keep it:

| Caller | Needs the wide match because |
|---|---|
| `PriceWalkingLoop._recover` (`loop.py`) | It runs when a submit/replace FAILED and the id that exists is precisely what is unknown — "what do you have under this key?" is the whole question. It also detects `len(working) > 1`, the runaway duplicate case spec §9 names, which is inherently a property of the wide match. |
| `ExitExecutor._await_settled` (`watchdog.py`) | Narrowed here, at the read. |

**`DuplicateOrderGuard` does NOT have this gap.** Checked explicitly, because
[C2](2026-08-20-unmappable-orders-reach-reconciliation.md) found the unmappable drop
reaching both call sites:

- The guard runs **before a brand-new submission**, when no broker order id exists
  yet. There is no specific order to scope to; "is there ANY order under this key?"
  is the question it is asked, so the wide match is correct rather than lax.
- Its direction is the opposite of `_await_settled`'s. It BLOCKS on any WORKING,
  FILLED, or unmappable row and permits only when everything under the key is DEAD.
  Narrowing it would be the dangerous direction.

What the guard does share is the underlying *completeness* hazard: if the broker
silently omits a working MAPPABLE order, the guard sees only dead rows and permits
the submission. That is not addressable by granularity, and it is not addressable by
refusing — the guard cannot treat "this answer might be incomplete" as a conflict
without blocking every crash-recovery re-entry, which is the exact behaviour the
deterministic identifier exists to permit. C2 closed the observed route (unmappable
rows). No further change is warranted here, and none is made.

## Coverage

`tests/test_watchdog_exit.py`, over the real `ExitExecutor` / `PriceWalkingLoop` /
`AuditedOrderExecution` stack, no network.

`PartialAnswerBroker` cancels the final rung to `Cancel Requested` — alive, able to
fill — and from that moment omits **that one row** while continuing to report the
walk's replaced-and-cancelled rungs. The test asserts the scenario really was the
partial one and not A3's: the visible rows are non-empty, every visible row is
terminal, and the omitted id is the only working order at the broker.

| Mutation | Caught by |
|---|---|
| Revert the narrowed check to the list-wide one (the defect) | `test_a_partial_broker_answer_refuses_the_cross_even_though_it_is_not_empty` — reproduces the double-close: `CROSSED`, second broker order id routed on top of the one still at `Cancel Requested` |
| Never confirm (treat every answer as uncertain) | `test_every_rung_present_and_terminal_still_confirms_and_crosses` + the existing cross tests |

The mutation was run: exactly one test failed, and A3's own empty-answer test and
the four existing cross tests passed unchanged under it — so the new test is
specific to the narrowing rather than to any change in the region.

The paired "still crosses" test pins the shape that must keep working, read from
the answer the settle window actually saw rather than from the account afterwards:
MORE THAN ONE order under the identifier, all terminal, the walk's own id among
them. Narrowing a confirmation is the kind of fix that makes a genuinely settled
case unresolvable, and that test is what stops it.

## Deploy

`src/` only — no config key, no migration. The image deploys alone, sequenced
alongside A3: same subsystem, same principle, same consequence. Pre-live-min-size.
