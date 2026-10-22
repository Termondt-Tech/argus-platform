# A submit refusal is not an absence (2026-09-04)

**Status: BUILT.** `src/` only — `execution/loop.py`, `execution/watchdog.py`,
`tools/desk.py`, and one constant made public in `execution/models.py`. **No new config key, no
migration, no audit category.** Purely a REPORTING change: no outcome, no retry
set, no allowance and no rail moves.

Closes the follow-up filed by
[the third live-fire day](2026-09-02-third-live-fire-day.md) finding 6, and the
task prompt
[`argus_submit_failure_broker_reason_prompt.md`](../tasks/argus_submit_failure_broker_reason_prompt.md).

## Why now

The prompt called it *"Not urgent… only the explanation is wrong."* That was true
of the 2026-09-02 measurement. It stopped being true on 2026-09-03:

| | 2026-09-02 | 2026-09-03 |
|---|---|---|
| failed entry attempts reported as `no_order_exists` | 7 of 8 | **5 of 7 (71%)** |
| `reject_reason` on those | `null` | `null` |
| exit path | — | **one of three sends** spent the same way |

`tools.desk`'s execution table for 2026-09-03 is five identical rows: every
unfilled trade idea died as `no_order_exists`, with no reason, and the two that
filled needed zero reprices. An operator reading that page cannot tell a permanent
`instrument_validation_failed: Trading of SPXW <symbol> is not supported` — cert
refusing a strike that exists in production's chain, since `[marketdata] source =
"live"` — from a transient `timeout_error` or a 502. The first can never succeed
on retry; the second might.

It also stopped being purely cosmetic. On the exit path `NOTHING_AT_BROKER` is
retryable under `_EXIT_SEND_ATTEMPTS`, so an unreported permanent refusal now
*spends budget* — three sends against a strike the venue will never accept.

## The bug, in one sentence

`_recover(state, what, exc)` has the causing exception in scope for its entire
body and threw it away at the one branch that needed it. `by_identifier` can
report the ABSENCE of an order; only `exc` can say why there is one.

```python
return await self._finish(
    state, WalkOutcome.NO_ORDER_EXISTS,
    reason=f"{what} failed and no order exists under this identifier",  # fixed template
)
```

## The fix

`WalkResult.mutation_error: str | None`, populated from `_broker_text(exc)` —
`f"{type(exc).__name__}: {exc}"`, bounded at `models.MAX_REJECT_REASON_CHARS`
(made public rather than duplicated; the two bounds cannot drift). It reaches
`walk.finished`, `entry.finished`, `ExitReport`, `desk`, and the four per-position
alert surfaces.

Six decisions:

**1. A dedicated field, not folded into `reason`.** The prompt asked for this if
an alert sweep existed to govern it. It does (2026-09-02), so the field joins the
governed set rather than becoming a fifth ungoverned string.

**2. Two fields and two keys, never one doing double duty.** The broker refuses an
order in two different ways: `reject_reason` is what it said about an order it
ACCEPTED and then rejected; `mutation_error` is what it said about one it would not
accept at all. `reject_reason: "timeout_error: Request Timed Out"` would read as
*the broker rejected this order*, which is the opposite of what happened.
`_alert_mutation_error` is `_alert_reject_reason`'s sibling for the same reason.

**2b. `tools.desk` prints it, because that is the surface the gap was SEEN on.**
The prompt scoped the change to `loop.py`; the 71% was read off desk's execution
table, five identical `no_order_exists` rows with nothing separating them. The
reason prints BELOW the table rather than as a column — broker prose is long and
the operative question is *which of these are the same cause*, which reads as a
list. `p.get("mutation_error")` deliberately, so a day spanning the deploy still
renders, with the section absent rather than showing `None`.

**3. "Present only on `NO_ORDER_EXISTS`" is held by a GATE in `_finish`**, one
line below `reject_reason`'s and deliberately separate from it — a rejection and a
refused submit are different events and neither implies the other. Enforced where
the value is assembled, so a caller passing one on the wrong outcome cannot put it
there. **That gate has no production caller that would violate it**, which is
exactly why it is unit-tested directly: a contract nothing exercises is a comment,
and it survived a mutation until a test drove `_finish` itself.

**4. Named `mutation_error`, in the codebase's own vocabulary.** The task prompt
suggested `submit_error` in passing (*"e.g."*), and it was wrong for half the
reach: `_recover` is shared, so a failed REPRICE lands here too, and a field named
for one of its two call sites is the kind of name that goes quietly false. This
module already has the word — *"Reads are retried freely; mutations never"* (module
docstring), *"A mutation failed. Ask the broker what exists"* (`_recover`) — and it
covers a submit and a replace exactly. `reason` still names which one
(`"submit failed …"` / `"replace failed …"`); the field carries the broker's words,
not the call's identity.

**Renamed before the first deploy, deliberately.** This is an audit payload key: no
row had been written with the old name yet, so the change cost a mechanical pass.
After a deploy it would have cost the log two names for one concept forever, and
`desk` a fallback read.

**5. The TYPE rides with the message**, the way every `error_type`/`error` pair in
this codebase's audit payloads already does, so a log filter is possible without
anyone matching prose.

**6. Nothing decides on it.** `Order.reject_reason`'s rule inherited whole, and
enforced: an AST test refuses any `Compare` with `mutation_error` as an operand and
any `.startswith`/`.find`/`__contains__` on it, across `loop.py`, `watchdog.py`
and `entry.py`. Distinguishing permanent from transient is a separate and larger
decision — and building it by matching this text is the tempting wrong fix, the
same one `entry.py`'s standing reject-reason prohibition already names.

## A hole found in the sweep that was supposed to govern this

Extending the 2026-09-02 alert sweep to a second field exposed that it was not
enforcing the first one either. `_resolve_detail` set `carries_payload` on **any**
`**` in the alert dict. `_record_exit`'s hand-built alert unpacks
`**spread.credit_payload()` — four price fields that can never carry a broker
reason — so the one surface the whole rule was written for was passing on an
unrelated splat. **Deleting `reject_reason` from it left the suite green.** It
carried the key and was correct in fact; the sweep was not enforcing it.

Tightened: `carries_payload` now requires the `**` to be a `to_payload()` call,
which is what the docstring's own reasoning always licensed. Both repeating pages
and `_record_exit` now fail the sweep if either key is dropped.

**Residual looseness, AUDITED and not pending work.** The hatch still accepts any
`to_payload()` rather than specifically a `WalkResult`'s, so in principle a future
alert could satisfy the rule by splatting an object that carries neither key. All
five governed sites were enumerated:

| site | how it satisfies the rule |
|---|---|
| `watchdog.py:_record_exit` | literal keys |
| `watchdog.py:_flatten_may_act` | literal keys |
| `watchdog.py:_page_if_spent_and_still_held` | literal keys |
| `entry.py:_report` (×2) | `**result.to_payload()` — a `WalkResult`, the right object |

**Nothing relies on the loose behaviour today**, so there is no follow-up filed.
This is a note for whoever next adds an alert that satisfies the rule by splatting:
check what is being splatted.

## Not done

- **`/status`'s spent-exit row carries neither field.** `SpentExitAttempt` has no
  `reject_reason` and now no `mutation_error`; only the internal `_SpentAttempt`
  (which feeds the two repeating pages) has them. Pre-existing, out of this
  commit's scope, and governed when it is done — the `_SEAMS` completeness test
  fails on any field added to the source without the converter naming it.
- **Any permanent-vs-transient distinction.** Explicitly a non-goal.
- **The `NO_ORDER_EXISTS` retry classification**, on either path. The entry/exit
  asymmetry (entry does not retry it; the exit does, through
  `DuplicateOrderGuard`, under a 3-send budget) is unchanged.

## Tests

Six new (`tests/test_execution_loop.py` S1–S4, `tests/test_desk.py` S5–S6 — the
second covering a pre-deploy row that lacks the key) plus the sweep extension and
one updated closed key-set assertion in `tests/test_entry.py` — which caught the new
key arriving in `entry.finished`, which is what that closed set is for.

**Nine mutations run, all nine caught** after the gate test was added: dropping
the exception at `_recover`; dropping the field from `to_payload`; removing the
`_finish` outcome gate (**survived until S3 drove `_finish` directly**); dropping
the exception type from `_broker_text`; dropping the key from the exit alert;
dropping it from both repeating pages; branching on the text; and desk dropping it
on either the read or the render.

Suite: **1955 passing, up from 1949.**
