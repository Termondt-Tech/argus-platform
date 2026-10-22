# Unmappable broker orders reach reconciliation (2026-08-20)

**Status: FIXED. Pre-live-min-size blocker, cleared.**

`TastytradeOrderExecution.get_live_orders()` silently dropped any broker order it
could not map to an `Order`, on a comment asserting that the reconciliation rail
did not read that call. The comment was **true when it was written and false by
the time anyone relied on it.** One unrecognized status string was enough to hide
an unknown WORKING or FILLED order from the one rail whose entire premise is
"halt on anything unexplained".

The bug is worth recording less for the defect than for its **shape**, which is
the part that generalizes: nothing changed in the code that was wrong. A new
caller appeared next to it, and a safety argument written for a different set of
callers quietly stopped covering the facts.

## What the code said

```python
async def get_live_orders(self) -> list[Order]:
    """Map today's live orders, skipping any this app cannot represent.
    ...
    Skipping is safe *here specifically* because this is not the reconciliation
    rail: spec §6 reconciles POSITIONS against the broker, and that path
    (`argus.reconcile`) is untouched by anything dropped here. Every skip is
    logged with the broker's order id so it is never silent.
    """
```

Every clause of that was accurate when chunk 1 shipped. Reconciliation was
position-only, so the only consumers were chunk 3's price-walking loop looking up
its own order and the duplicate guard looking up its own identifier — for both of
which a foreign order in another instrument is genuinely irrelevant, and for
neither of which is failing the whole read on one bad row acceptable.

Chunk 2 then added order-level reconciliation. `reconcile.Reconciler.reconcile`
calls `get_live_orders()` and feeds the answer straight into `reconcile_orders`.
Nobody revisited the comment, because nothing about the comment's own module had
changed. It went on stating a false premise in an authoritative voice, right next
to the line that made it false.

## The three consumers, and what the drop did to each

| Consumer | What the drop meant |
|---|---|
| `reconcile.Reconciler` | An unknown WORKING or FILLED order invisible to the rail built to halt on it. The reconcile came back **clean** and the process resumed trading against an account holding something it could not read. |
| `DuplicateOrderGuard` | The only duplicate-order protection in the system. An unmappable working order under our own `external_identifier` read as *no order at all*, so a second one would go out — spec §9's runaway case, against the one module that exists to prevent it. |
| `loop.LiveOrderView` | An order absent from the answer reads as absent from the broker. `by_id` feeds the blind-cycle counter; `by_identifier` tells the recovery path nothing was ever sent. Tracked separately as A3, which is a distinct interpretation bug about a genuinely-missing order — but the drop is how a *present* order got into that path. |

A fourth caller, `argus.tools.order_smoke`, is a manual tool; it printed a
short list and said nothing about what it had refused.

The skip was logged at WARNING: journal only. **No audit record, no alert.**
Nothing surfaced it on a phone, in `/status`, or in the hash-chained trail — so
the condition that would hard-halt the next morning's startup was invisible until
the next morning's startup.

## The fix

**Stop dropping.** `get_live_orders()` returns `LiveOrders`, a result object with
two fields — `orders` (mappable) and `unmappable` (`UnmappableOrder` records
carrying the broker's id, `external_identifier`, underlying, RAW status string and
the mapper's reason).

Three properties of that type are deliberate:

- **It is a result object, not a list.** A plain list has nowhere to put "and
  there were three more I could not read", so the drop *had* to be silent. That is
  the shape of the bug, not an implementation detail of it.
- **It defines no `__iter__`.** A caller cannot write `for o in live_orders` and
  receive a quietly filtered view; it has to name `.orders` or `.unmappable`,
  which is the moment it has to think about both.
- **`UnmappableOrder` is deliberately not an `Order`.** Nothing can submit it,
  reprice it or classify it, because none of those questions has an answer.
  Inventing a defaulted status or a guessed direction is precisely what
  `UnmappableOrderError` exists to prevent.

`reconcile_orders` takes `unmappable` as a **keyword-only argument with no
default**. A default of `()` would let the next caller reconstruct the original
bug by simply not knowing the argument existed.

### Where it halts, and where it only announces

Reported explicitly because the task asked for the choice, not a default:

- **Restart-time reconciliation HALTS.** A new discrepancy kind,
  `unmappable_broker_order`, rides in the existing `ReconciliationResult`, so it
  inherits the environment gate, the alert and the refusal without a second
  implementation of any of them. Sandbox/live hard-halt; shadow has no execution
  port and never sees it.
- **Mid-session observation ANNOUNCES.** `AuditedOrderExecution.get_live_orders()`
  writes a Tier 1 `order.unmappable_live` record and raises an
  `unmappable_broker_order` alert, deduped on (broker id, raw status) because the
  price-walking loop polls this every cycle.

The asymmetry is the same one `fills.py` already draws, and it is not a
relaxation. Mid-session, an unmappable order is not by itself proof that anything
of Argus's is wrong — a hand-placed order in another instrument with a status the
mirrored enum lags on is real and benign — and halting the process on it would
take the **watchdog** down with it, abandoning positions Argus certainly does
hold. At restart the premise is different: "refuse to resume on anything
unexplained", and an unreadable order is the definition of unexplained. In
between, the duplicate guard blocks the specific identifier, so nothing doubles
while a human reads the alert.

### Two sub-decisions worth not relitigating

**`known_order_ids` does not suppress an unmappable order.** The other two
discrepancy kinds ask "did Argus place this?", and a ledger row is a complete
answer to that. This kind asks "can Argus *read* this?", and a ledger row says
nothing about it. An order of ours wearing a status we cannot parse is exactly as
unexplained as a stranger's. The accepted cost is real: one benign foreign order
with a status tastytrade added since the enum was last checked will hard-halt a
sandbox/live startup. That is the correct direction, it is loud, and the audit
record carries the raw string — which is normally a one-line fix to `OrderStatus`.

**`LiveOrderView` raises rather than reporting absence.** Both its methods answer
a question the walk acts on, and for both, omitting an order the broker *does*
hold is the dangerous direction. An unmappable order carrying the id or identifier
being asked about raises `UnmappableOrderError`, which `_observe`/`_recover`
already treat as a broker that cannot answer: retried, escalated to
`BrokerUnavailable`, walk ends UNRESOLVED. Bounded and loud, and it beats
repricing — or re-sending — against a state nobody read. Unmappable orders
matching neither query are not this view's business.

## What is not fixed here

A3 — `_await_settled`'s interpretation of an order that has genuinely gone
missing — is a separate bug and stays separate. This change removes the way a
*present* order got into that path; it does not change what happens once an order
is actually absent.

**Fixed separately the next day**, as its own change:
[an empty broker answer is not a verdict](2026-08-21-empty-broker-answer-is-not-a-verdict.md).

## Coverage

Six mutations were run, each reverting one part of the fix, and each was caught:

| Mutation | Caught by |
|---|---|
| Adapter drops unmappable orders again (the original defect) | `test_get_live_orders_carries_the_unmappable_instead_of_dropping_it`, `test_an_unmappable_order_halts_sandbox_startup_through_the_real_adapter` |
| Guard ignores `LiveOrders.unmappable` | `test_an_unmappable_order_under_our_identifier_blocks` + 1 |
| `reconcile_orders` drops them | 4 tests, including the end-to-end halt |
| `reconcile_orders` suppresses them when the id is known | `test_an_unmappable_order_is_a_discrepancy_even_when_argus_placed_it` |
| `LiveOrderView` reads unreadable as absent | `test_by_id_raises_rather_than_reporting_our_own_order_absent` + 1 |
| Audited layer stops announcing | `test_an_unmappable_order_gets_a_tier_1_record_and_an_alert` + 1 |

The end-to-end halt test is deliberately built on the **real**
`TastytradeOrderExecution` over an SDK-shaped stub account, not on
`FakeOrderExecution`: the drop lived in the adapter, so a test starting downstream
of it would prove nothing about the fix and would survive the mutation.

Regression coverage on the common path is explicit
(`test_a_readable_order_still_reconciles_through_the_real_adapter`) — a false halt
here would brick every startup.

## The generalizable rule

**A safety comment that names the callers it is safe for expires the moment a
caller is added.** The premise here was checkable and nobody checked it, because
the change that falsified it was in a different file and looked unrelated.

Whoever adds the next caller of `get_live_orders()`: `LiveOrders.unmappable` is
not optional detail, it is the reason that type exists. The type is shaped to make
you answer for it — no `__iter__`, and a required keyword argument downstream —
but the shape only buys a prompt, not a decision.
