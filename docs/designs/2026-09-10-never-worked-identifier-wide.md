# A rung that never worked is not a verdict either

**Status: SHIPPED 2026-09-10.** Signed off and built the same day. All three §9
confirmations were put to Richard and all three recommendations were taken — §3.1 (the
gate moves to `_finish` and `_resolve`'s call is deleted), §3.2 (`WALKED_AWAY` and
`DRIFTED` are in the set), §3.3 (`probe=False` as an explicit keyword, default `True`).
**Nothing was overruled at sign-off; one claim was overturned on contact with the code
— see "As built" below.** Every §4 item landed as written, all fourteen §6 tests exist,
and all nine §7 mutations produce the predicted result.

Finding:
[a rung that never worked is not a verdict either](../decisions/2026-09-10-never-worked-is-not-a-verdict.md).
Sibling that fixed the same rule one branch over:
[a terminal rung is not a verdict](../decisions/2026-09-02-a-terminal-rung-is-not-a-verdict.md).
Evidence: the 2026-09-09 cert session (12 trade ideas, 2 fills, 7 refused on
`instrument_validation_failed`).

> **Line numbers** are against `3a9358f`. All `loop.py` unless said otherwise.

---

## 1. Recommendation in one paragraph

**Move the identifier-wide FILLED probe from `_resolve` into `_finish`, and gate it on
the OUTCOME rather than on the branch that produced it.** `_finish` is the sole
constructor of every `WalkResult` in the module, so it is the only place where *no
`WalkResult` may report a non-fill without the identifier having been asked* is an
invariant rather than a habit. Three call sites conclude a walk today without asking —
`_await_in_market`'s two deadline branches and `_walk`'s blind-cycle exit — and the
codebase has now failed to hold this rule by discipline three times (2026-08-26 twice,
2026-09-02 once). Enforcing it at the constructor makes the fourth instance
unreachable instead of unlikely. The change is: a frozen `_ASK_BEFORE_CONCLUDING`
outcome set, a `probe: bool = True` keyword for the three sites that have *just* asked
the same question, `_resolve`'s own call deleted as now redundant, and one test that
fails when a new `WalkOutcome` member joins the enum without being classified.

Rejected: patching the two or three implicated branches in place (§3.1, option A) —
it is the cheapest change and it is the one that guarantees a fourth instance.

The cut line offered at sign-off — dropping `WALKED_AWAY` and `DRIFTED` from the set —
was **not taken** (§3.2). The set is uniform.

---

## 2. What is broken, in source

### 2.1 The deadline branch concludes without asking

`_await_in_market` (`1159`) runs **after every reprice**, not only after the first
submit. `_walk`'s own comment says why (`1077`):

> the cadence clock does not start until the order is genuinely in the market — after
> the first submit AND after every reprice, since a replace mints a new order that has
> to get there on its own.

So a walk at step 1 re-enters it holding the replacement, while the original rung is
still resolvable at the broker under the same `external_identifier`. Inside, there are
three exits and only one asks:

| Exit | Line | Asks the identifier |
|---|---|---|
| terminal status observed → `_resolve` | `1177` | ✔ since 2026-09-02 |
| deadline, order never seen → `UNRESOLVED` | `1192` | ✘ |
| deadline, seen but never in market → `NEVER_WORKED` | `1200` | ✘ |

`_observe` reads `state.broker_order_id` — the CURRENT rung, by id. A replacement that
sits unreadable or merely non-`Live` for the full `working_timeout_s` takes a deadline
branch, and the deadline branch concludes about the spread from a fact about one rung.

**2026-09-02 did not overlook this case; it scoped itself to a different input.** Its
probe hangs off a *terminal broker observation* and argues from exactly that
(`_finish_if_filled_elsewhere`, `1317`): the read *"is strictly ADDITIVE over a
positive terminal observation the broker gave us one call ago."* Here there is no such
observation — the walk reaches a terminal conclusion of its own, off a clock, with the
broker having said nothing conclusive. **The trigger for asking should be that the
WALK is about to conclude, not that the BROKER already did.**

### 2.2 It is three sites, not one

| Site | Line | Outcome | Why it concludes |
|---|---|---|---|
| `_await_in_market`, seen | `1200` | `NEVER_WORKED` | `working_timeout_s` expired |
| `_await_in_market`, not seen | `1192` | `UNRESOLVED` | `working_timeout_s` expired |
| `_walk`, blind cycles | `1106` | `UNRESOLVED` | N unreadable `by_id` answers in a row |

The blind-cycle exit deserves the probe most obviously of the three: it concludes from
repeated failure of `by_id`, and `by_identifier` is a **different question against a
different key**. A fill on the ORIGINAL rung carries a different `broker_order_id`
entirely, so every `by_id` read can legitimately fail while the identifier-wide answer
is sitting there.

And `UNRESOLVED` is the louder of the two outcomes: `entry.py`'s `_ALERTING_OUTCOMES`
(`223`) names it the headline "a human should look at this now" verdict, and on the
exit path it lands in `ExitOutcome.NOT_RESOLVED`. Fixing `NEVER_WORKED` alone would
leave the noisier sibling wrong.

### 2.3 The codebase already contains the argument

`watchdog.py:3727`, defending the one-attempt rule:

> Its worst case is measured, not theoretical: `NEVER_WORKED` — the
> `working_timeout_s` path — calls `_request_cancel`, which is best-effort and
> confirms nothing, **so the abandoned order is at `Cancel Requested` at best and can
> still fill.**

Correct, and the corollary was never drawn. *If a `NEVER_WORKED` rung can still fill,
then by the time the walk concludes it may already have.* The sentence that justifies
refusing a **retry** also demands a **read**, and the two are not in tension: one is
about sending, the other about asking.

### 2.4 Reproduced

`20260909-call-7660-7670`, entered 13:36:42 ET. The walk reported
`never_worked / filled_contracts 0 / steps 1 / final_limit_price 0.50`. The ledger
holds **1 contract at a 0.55 credit**, closed intraday at 0.15 for +40.00; both
`tools.desk` and `tools.mark` reconstruct fills from `order_legs` by
`external_identifier`, so the ledger is the authority and the walk is the wrong
surface.

`_reprice` sets `state.sent = next_sent` (`1475`) and `_finish` reads
`final_limit_price` off `state.sent` (`1685`), so the reported `0.50` is the
**replacement's** limit and rung 0 was `0.55` — exactly the credit the ledger records.

That last step is an inference across two surfaces. What confirms it directly, and
what the fix should make unnecessary to infer: `walk.finished` carrying
`filled_contracts: 0` with `steps: 1`, against `order.fill` rows under the same
identifier naming a different `broker_order_id`, with no
`walk.filled_under_identifier` record.

---

## 3. The design questions

### 3.1 (Q1) Where does the probe go? — **`_finish`, and `_resolve`'s call is deleted**

> **DECIDED 2026-09-10: option B.** Verified against source before the call, and all
> three checks came back clean: `_unfilled_reason` has **exactly two callers**
> (`1242`, `1248`), both inside `_resolve` and both moving, so the lift has no
> fallout; there are **17 `_finish` call sites**, all in `loop.py` and all reachable,
> so the gate genuinely covers every exit from a walk; and `_first_filled` is already
> the single shared definition with a documented FIRST-not-largest contract, so the
> gate inherits the right semantics with nothing new to specify.

**Option A — patch the branches.** Call `_finish_if_filled_elsewhere` at `1192`,
`1200` and `1106`. Three lines, no contract changes, lowest mutation surface.

**Option B — gate `_finish` on the outcome. RECOMMENDED.** `_finish` (`1671`) is the
sole constructor of `WalkResult` in this module. Every path out of a walk goes through
it. That makes it the only site where the rule is an **invariant** rather than a
convention, and the convention is what has failed:

| Instance | Date | Where the rule was applied one branch short |
|---|---|---|
| `reconcile._ask_broker` | 2026-08-26 | mismatch-vs-unreachable |
| `entry._check_for_duplicate` | 2026-08-26 | same, entry path |
| `_resolve`'s terminal branch | 2026-09-02 | identifier-wide FILLED |
| **the deadline branches** | **now** | **identifier-wide FILLED** |

Option A fixes the fourth and licenses a fifth: the next author to add a `WalkOutcome`
or an early return has nothing telling them the question exists. Option B is the
[halt-grade](../decisions/2026-08-29-halt-scope-split.md) shape — classify by a
property, default the unclassified to the safe side, and pin it with a test that fails
on an unclassified member.

**Deleting `_resolve`'s call is part of the recommendation, not an extra.** Leaving it
gives the module two probe sites and double-probes every `REJECTED` — a second
identical GET milliseconds after the first. `_resolve` keeps choosing between
`REJECTED` and `CANCELLED_ELSEWHERE` and passes its base reason; `_finish` does the
asking and the reason qualification. The 2026-09-02 behaviour is preserved exactly;
only the frame it runs in moves up by one.

**Cost of B, stated plainly.** `_finish` becomes a function that can perform broker
I/O, which it cannot today. It is already `async` and already writes `walk.finished`,
so this is not a new kind of thing for it to do — but it is a new *bound* on the exit
path's terminating frame, which `_run_exit` awaits while a triggered 0DTE position is
open. That bound is one attempt, no retry, no reconnect (§3.4), which is
`_finish_if_filled_elsewhere`'s existing and deliberately-argued contract.

### 3.2 (Q2) Which outcomes ask?

> **DECIDED 2026-09-10: the set is uniform — `WALKED_AWAY` and `DRIFTED` are IN.**
> The offered cut was declined. One fact found while preparing the call sharpens what
> was bought and what was paid, and it is recorded below rather than lost.

The rule that decides every member at once: **ask wherever the walk concludes "no
fill" and an order it sent may still be live at the broker.**

```python
#: Outcomes that assert "this trade idea did not fill". Each one is a claim about the
#: SPREAD, and the walk holds facts about at most one RUNG — so the identifier is
#: asked before any of them is returned. Membership is by that property alone; adding
#: a WalkOutcome without classifying it fails `test_every_outcome_is_classified`.
_ASK_BEFORE_CONCLUDING = frozenset({
    WalkOutcome.NEVER_WORKED,
    WalkOutcome.UNRESOLVED,
    WalkOutcome.REJECTED,
    WalkOutcome.CANCELLED_ELSEWHERE,
    WalkOutcome.NO_ORDER_EXISTS,
    WalkOutcome.WALKED_AWAY,
    WalkOutcome.DRIFTED,
})
```

**Excluded, and each for its own reason — not one blanket one:**

- **`FILLED`** — it is the answer. Probing would ask a question already resolved.
- **`BLOCKED`** — nothing was sent, so there is no claim about a spread to be wrong
  about; and `_ask_guard` asked the identifier-wide question one frame earlier and
  acted on it. Two independent reasons, either sufficient.
- **`ABORTED`** — `_abort` (`1652`) is the kill-switch and session-close path. **A
  halt must not add a broker round trip.** The phone button's stated bound is one
  `cycle_wait_s`; a GET inside the abort frame widens it for no benefit, since nothing
  downstream acts on the abort's fill count. This exclusion is a safety rail, not an
  optimisation, and belongs in a comment saying so.

The exclusions are a set of their own, so a new `WalkOutcome` cannot be silently
absent from both (T10):

```python
#: Outcomes that must NOT ask, each for its own reason — see above. Not "everything
#: else": the union with `_ASK_BEFORE_CONCLUDING` must cover `WalkOutcome` exactly.
_NEVER_ASKS = frozenset({
    WalkOutcome.FILLED,
    WalkOutcome.BLOCKED,
    WalkOutcome.ABORTED,
})
```

**`WALKED_AWAY` and `DRIFTED` were the two arguable members, and they are IN.** Both
reach `_finish` immediately after a best-effort `_request_cancel` that confirms
nothing — structurally identical to `NEVER_WORKED`. Excluding them would re-assert
exactly the reasoning that produced this defect three times: *this branch is
different*.

**What the exit path already does, found while preparing the sign-off.** On the exit
side `WALKED_AWAY` is the **only** outcome that proceeds to the market-crossing
escalation — `_after_walk` (`watchdog.py:1303`) sends everything else to
`NOT_RESOLVED` — and the cross opens with `_await_settled`, which is itself an
identifier-wide FILLED read. So the gate's exit-side coverage of `WALKED_AWAY` is
**redundant, not wrong**, and the real gain is entry-side, where there is no
`_await_settled` equivalent and a walked-away entry whose prior rung filled is
recorded unfilled with nothing downstream to correct it.

**The cost, stated exactly:** one extra GET on the entry path's ordinary non-fill
outcome (on top of the cancel round trip it already makes), and one redundant GET on
the exit path immediately before `_await_settled` asks the same question. Paid
deliberately, to keep the rule uniform.

**A third option was offered and declined**: pass `probe=False` from the exit's
`WALKED_AWAY` path so only entry pays. It removes the redundancy by making the
exclusion depend on a *downstream caller's* behaviour that the walk loop cannot see —
a later change to `_after_walk` would silently invalidate it, which is this defect's
own failure mode (a comment true when written, false once a caller changed) wearing a
different hat.

### 3.3 (Q3) The three sites that have already asked — `probe=False`

> **DECIDED 2026-09-10: the explicit keyword, default `True`.** The `_Walk`-flag
> alternative was declined for the reason given below.

Three `_finish` calls sit within a few statements of a successful `by_identifier`
read. Re-asking is not additive there; it is the same question again, and on one of
them the broker has just refused it repeatedly.

| Site | Line | Outcome | Why not |
|---|---|---|---|
| `_recover`, >1 working | `1596` | `UNRESOLVED` | `by_identifier` answered; that answer IS the verdict |
| `_recover`, nothing found | `1636` | `NO_ORDER_EXISTS` | same read, positively empty — the whole basis of the outcome |
| `run()`, `BrokerUnavailable` | `940` | `UNRESOLVED` | `_recover` just spent `max_recovery_attempts` on this exact call and failed every one |

Expressed as an explicit keyword at those three call sites rather than as state on
`_Walk`:

```python
async def _finish(self, state, outcome, *, reason=None, mutation_error=None,
                  probe: bool = True) -> WalkResult:
```

**Default `True`, and it must stay `True`** — the default is the safe side, and a
future call site that forgets the keyword asks one unnecessary question rather than
skipping a necessary one. A `_Walk` flag was considered and rejected: "was the
identifier read recently enough" is a staleness question, and inventing a second
staleness rule inside the walk loop to save three keywords is a bad trade.

### 3.4 (Q4) The probe's own contract — unchanged from 2026-09-02, and verbatim

`_finish_if_filled_elsewhere`'s existing rules are the right ones and none is
weakened:

- **One attempt, no retry, no reconnect.** Its docstring's reasoning binds harder
  here: this runs on the EXIT path's terminating frame, awaited by `_run_exit` while a
  triggered 0DTE position is open, and `sum(_RECOVERY_BACKOFF_S)` of sleeping there
  buys a second look at a question whose failure costs nothing new.
- **A failure falls back to the verdict the caller already had.** It never manufactures
  `UNRESOLVED` out of a failed read. `state.probe_failed` is set, `walk.disconnected`
  is written with `call="terminal_probe"`, and the reason string is qualified by
  `_unfilled_reason` — which moves into `_finish` with the probe, so the caller passes
  a base reason and `_finish` composes.
- **FILLED only.** The question is *"did anything under this key fill?"*, never *"what
  else is out there?"*. Widening it toward working orders re-opens the double-send
  reasoning `_await_settled` exists to hold.
- **`_first_filled` stays the one implementation.** It is already shared by `_recover`
  and `_finish_if_filled_elsewhere` for the stated reason — *"which order counts as
  having left a position" is one question and must have one implementation.*

### 3.5 (Q5) Ordering against `_request_cancel`, and what moves on the state

**The probe runs after the cancel, not before**, which is what falls out of putting it
in `_finish` — `NEVER_WORKED`, `WALKED_AWAY`, `DRIFTED` and `ABORTED` all cancel and
then finish. That ordering is correct on its own terms: cancelling the current rung is
right regardless of what a prior rung did, and probing last makes the answer as fresh
as the frame allows. A cancel cannot hide a fill on a different rung.

State mutation on a positive probe is 2026-09-02's, unchanged:

- `state.last` and `state.broker_order_id` **move** onto the filled order, so
  `filled_contracts` reports the fill instead of the abandoned rung's `0`. Moving the
  outcome without moving these is the half-applied fix that is harder to spot than the
  bug.
- `state.sent` **does not move.** `final_limit_price` is read off it, and relabelling
  it as the filled rung's limit dresses one limit up as more fill-like than another —
  a limit is not a fill price.
- `state.cancel_requested` **does not move.** A cancel *was* requested; the record
  should keep saying so.

### 3.6 (Q6) What the operator sees

`walk.filled_under_identifier` (Tier 2) already exists and is reused, with one field
added — **the outcome that was about to be returned**:

```python
"suppressed_outcome": str(outcome),        # NEW: never_worked / unresolved / rejected / …
"terminal_broker_order_id": …,             # None on the deadline branches
"terminal_status": …,                      # None on the deadline branches
"filled_broker_order_id": …,
"filled_contracts": …,
"steps": …,
```

Without that field the two shapes collapse into one category and the 2026-09-02 rate
(1-of-3 on successful replaces) can no longer be read separately from this one. The
headline number in both incidents is a **rate**, and prose is not countable — which is
why the category exists at all.

Three fields go `None` on the deadline branches because there is no terminal
observation. That is information, not a gap: `terminal_status: null` **is** the
signature of this defect versus 2026-09-02's.

`walk.finished`'s `reason` carries the correction in prose, as it already does:
`"no confirmed in-market status within 60s, but 1430512 under this identifier is
filled"`.

### 3.7 (Q7) Can the probe see a fill from an EARLIER walk? — no, and here is why

Widening the gate from one branch to seven outcomes widens the blast radius of a false
`FILLED`, so this was checked before sign-off rather than assumed.

`external_identifier` is per trade idea, and two live paths deliberately put more than
one order under one key (`_finish_if_filled_elsewhere`'s own docstring names them):
`_recover` resuming against an order a "failed" submit actually landed, and **the exit
retry re-running a whole walk under the same `argus-close-*` key.** The second is the
one that could in principle hand a later walk a previous walk's fill.

It cannot, because of what licenses the retry: `_RETRYABLE_EXITS` holds exactly
`NOTHING_AT_BROKER` (`NO_ORDER_EXISTS`) and `CROSS_UNPRICEABLE`, **both of which mean
nothing filled** — the first is a positively-empty broker answer, the second is
reachable only through `_await_settled`'s positive confirmation that the abandoned
order is terminal. A walk that filled never produces either, so no second walk under
that key can inherit a fill.

`_recover`'s case is within one walk and is the condition the probe exists for.

**This is a property of the retry taxonomy, not of the gate.** It holds today and it
held for 2026-09-02's narrower version. If `_RETRYABLE_EXITS` ever gains a member that
can follow a partial fill, this reasoning has to be redone — which is worth a sentence
in the code beside the set, and is noted in §5.

---

## 4. The change, file by file

**`loop.py` only.** No other module changes.

1. **`_ASK_BEFORE_CONCLUDING`** — module-level frozenset with the docstring in §3.2,
   placed beside `WalkOutcome` so the two are read together.
2. **`_finish` (`1671`)** — gains `probe: bool = True`. Before building the result:
   if `probe and outcome in _ASK_BEFORE_CONCLUDING`, run the identifier read; on a
   positive fill, move `state.last` / `state.broker_order_id`, write
   `walk.filled_under_identifier` with `suppressed_outcome`, and build a `FILLED`
   result with the corrected reason; on a failed read, set `state.probe_failed` and
   qualify the caller's reason through `_unfilled_reason`; otherwise proceed unchanged.
3. **`_finish_if_filled_elsewhere` (`1287`)** — its body becomes `_finish`'s probe
   block. The method is removed rather than kept as a wrapper: two names for one step
   is how the module ends up with two probe sites again.
4. **`_resolve` (`1219`)** — the `resolved = await self._finish_if_filled_elsewhere(…)`
   call and its early return are deleted; the branch passes its base reason to
   `_finish` and lets the gate do the asking. `REJECTED` / `CANCELLED_ELSEWHERE`
   selection is untouched.
5. **`_unfilled_reason` (`1400`)** — moves into `_finish`'s composition. Same string,
   same rule, one caller.
6. **Three `probe=False` keywords** at `940`, `1596`, `1636`, each with a one-line
   comment naming the read that already happened.
7. **Recursion guard** — the probe block builds its `FILLED` result directly rather
   than re-entering `_finish`. `FILLED` is not in the set, so a re-entry would
   terminate anyway; building it directly makes that a fact rather than a coincidence.

### 4.1 `_finish`'s shape

Illustrative, not literal — the audit payload and result construction already exist
below the inserted block and are unchanged.

```python
async def _finish(
    self,
    state: _Walk,
    outcome: WalkOutcome,
    *,
    reason: str | None = None,
    mutation_error: str | None = None,
    probe: bool = True,
) -> WalkResult:
    """...

    `probe` defaults to True and MUST stay that way: the default is the safe side,
    so a call site that forgets it asks one unnecessary question rather than
    skipping a necessary one. Pass False only where `by_identifier` has just been
    read and that read IS the verdict — the three sites in §3.3, nowhere else.
    """
    if probe and outcome in _ASK_BEFORE_CONCLUDING:
        try:
            candidates = await self._view.by_identifier(state.sent.external_identifier)
        except Exception as exc:          # noqa: BLE001 — falls back; never raises
            state.recovered += 1
            state.probe_failed = f"{type(exc).__name__}: {exc}"[:200]
            await self._record_disconnect(state, what="terminal_probe", exc=exc, attempt=1)
        else:
            if (filled := _first_filled(candidates)) is not None:
                self._audit.telemetry("walk.filled_under_identifier", {
                    "external_identifier": state.sent.external_identifier,
                    "suppressed_outcome": str(outcome),
                    "terminal_broker_order_id": state.last.broker_order_id
                        if state.last is not None else None,
                    "terminal_status": str(state.last.status)
                        if state.last is not None else None,
                    "terminal_reject_reason": state.last.reject_reason
                        if state.last is not None else None,
                    "filled_broker_order_id": filled.broker_order_id,
                    "filled_contracts": filled.reported_fill_quantity,
                    "steps": state.steps,
                })
                prior = state.last
                state.last = filled              # `state.sent` deliberately does NOT move
                state.broker_order_id = filled.broker_order_id
                outcome = WalkOutcome.FILLED
                reason = _corrected_reason(reason, prior, filled)
                mutation_error = None            # not a refusal any more
        if outcome is not WalkOutcome.FILLED:
            reason = self._unfilled_reason(state, reason)

    # ...existing WalkResult construction and `walk.finished` record, unchanged...
```

Three details a build session should not have to re-derive:

- **`state.last` may be `None`** on the `not seen → UNRESOLVED` branch and on the
  blind-cycle branch. The three `terminal_*` fields are `None` there, which is the
  signature that distinguishes this defect from 2026-09-02's (§3.6) — so they must be
  emitted as `None`, not omitted.
- **`mutation_error` is cleared** when the probe converts the outcome. Its contract is
  *"present only on `NO_ORDER_EXISTS`"*, enforced by a gate further down `_finish`;
  leaving it set alongside `FILLED` would fight that gate. `NO_ORDER_EXISTS` only ever
  arrives here with `probe=False`, so in practice this line is belt-and-braces — write
  it anyway, and let the existing gate be the thing that is tested.
- **`_unfilled_reason` now takes a `str | None`** (several call sites pass no reason).
  Today both its callers pass a non-empty base. Preserve today's output exactly when
  the probe answered and found nothing: that is `reason` unchanged, not a rewritten
  string.

### 4.2 Every `_finish` call site, classified

The builder should not have to re-derive this. Seventeen sites, against `3a9358f`:

| Line | Outcome | Gate |
|---|---|---|
| `932` | `BLOCKED` | never asks (set) |
| `940` | `UNRESOLVED` | **`probe=False`** — `_recover` just spent every attempt on this call |
| `1105` | `UNRESOLVED` (blind cycles) | **asks — NEW** |
| `1146` | `REJECTED` (`_first_submit`) | asks |
| `1192` | `UNRESOLVED` (not seen) | **asks — NEW** |
| `1199` | `NEVER_WORKED` | **asks — NEW, the reported defect** |
| `1231` | `FILLED` | never asks (set) |
| `1240` | `REJECTED` | asks — was `_resolve`'s probe, now the gate's |
| `1246` | `CANCELLED_ELSEWHERE` | asks — was `_resolve`'s probe, now the gate's |
| `1267` | `FILLED` (partial) | never asks (set) |
| `1384` | `FILLED` (probe's own) | removed — absorbed into `_finish` |
| `1443` | `DRIFTED` | **asks — NEW** |
| `1449` | `WALKED_AWAY` | **asks — NEW** |
| `1573` | `FILLED` (`_recover`) | never asks (set) |
| `1595` | `UNRESOLVED` (>1 working) | **`probe=False`** — that read IS the verdict |
| `1635` | `NO_ORDER_EXISTS` | **`probe=False`** — positively-empty read is the outcome |
| `1669` | `ABORTED` | never asks (set) — **safety rail, §3.2** |

`1146` is worth one note: `_first_submit`'s `REJECTED` gains the probe under the
uniform rule, where 2026-09-02 deliberately excluded it because both production callers
run `DuplicateOrderGuard` immediately before that submit and the guard blocks on
`FILLED`. That exclusion is still *true*; it is now expressed as one redundant GET
rather than as a carve-out, which is the trade §3.1 makes everywhere. If the redundancy
is unwanted, `probe=False` there is a one-word change with the guard's own reasoning as
its comment — but it is **not** recommended, because "a caller ran the guard first" is
again a property of callers that the loop cannot see (§3.2's declined third option).

---

## 5. What is deliberately NOT changed

- **The one-attempt rule.** This changes what a walk CONCLUDES, never what it SENDS.
  Every sentence of `watchdog.py`'s defence of the rule survives intact, including the
  `NEVER_WORKED` sentence quoted in §2.3 — which is now the argument for the read
  rather than an unexploited observation.
- **`_RETRYABLE_EXITS` and the exit taxonomy.** A walk that now reports `FILLED` never
  reaches `ExitOutcome.NOT_RESOLVED`, so it never spends the attempt and never fires
  `_SPENT` — `_after_walk` (`1277`, `watchdog.py`) handles `FILLED` first. Nothing in
  the taxonomy needs to learn anything, and the open
  [confirmation-gated re-attempt](../decisions/README.md#open-decisions) decision is
  neither advanced nor narrowed.
- **`filled_contracts`' across-rungs under-count.** It reports one broker order's fill
  while `requested_contracts` is the whole idea's. This fix moves *which* order it
  reports; it does not merge rungs, and the `partially_filled` scope straddle is
  untouched.
- **`entry_credit`.** Realized per-leg since migration 0005, independent of the walk.
- **`_ALERTING_OUTCOMES`.** Its membership is right; the bug was feeding it a wrong
  outcome.
- **The guard.** `_ask_guard`, `guard_first_submission`, and the rule that the retry is
  on the QUESTION and never the verdict are all untouched.
- **`_RETRYABLE_EXITS`.** Unchanged — but §3.7's safety argument *depends* on its two
  members both meaning "nothing filled". A one-line comment goes beside that set saying
  so, because a future member that can follow a partial fill would let a second walk
  under one `argus-close-*` key inherit the first walk's fill. That is the only way the
  gate can produce a false `FILLED`, and it is closed today.

---

## 6. Tests

| # | Asserts |
|---|---|
| T1 | `NEVER_WORKED` at the deadline with a FILLED order under the identifier → `WalkResult.outcome is FILLED`, `filled_contracts` from the filled order, `final_limit_price` still the last rung's |
| T2 | Same for the `not seen` → `UNRESOLVED` branch |
| T3 | Same for `_walk`'s blind-cycle `UNRESOLVED` |
| T4 | `WALKED_AWAY` and `DRIFTED` probe, and convert to `FILLED` over a filled prior rung |
| T5 | Probe raises → the caller's outcome is preserved, `reason` is qualified, `walk.disconnected` written with `call="terminal_probe"`, no exception escapes |
| T6 | `ABORTED` performs **no** broker read — asserted on the view's call count, not on the result |
| T7 | `BLOCKED` performs no broker read |
| T8 | `probe=False` honoured at all three sites: `_recover`'s two finishes and `run()`'s `BrokerUnavailable` handler make exactly one `by_identifier` call between them |
| T9 | `walk.filled_under_identifier` carries `suppressed_outcome`, and `terminal_status` is `None` on a deadline-branch fire and populated on a `_resolve` fire |
| T10 | **`test_every_outcome_is_classified`** — `_ASK_BEFORE_CONCLUDING \| _NEVER_ASKS == set(WalkOutcome)` and the two are disjoint. A new member fails on both halves until classified |
| T11 | 2026-09-02's existing regression (terminal rejection over a filled original) still passes through the moved gate, **unedited** |
| T12 | Exit path end-to-end: a `NEVER_WORKED`-over-a-fill exit yields `ExitOutcome.FILLED`, the attempt is **not** spent, `_SPENT` does not fire |
| T13 | `state.last is None` (the `not seen` and blind-cycle branches) → the three `terminal_*` fields are emitted as `None`, not omitted, and nothing raises |
| T14 | A probe that answers with **no** fill leaves `reason` byte-identical to today's for both `_resolve` outcomes — the moved `_unfilled_reason` did not rewrite the happy path |

T10 is the test that makes this design worth more than option A — it is the whole
difference between an invariant and a habit. T6 keeps the halt path honest. T13 covers
the two branches where `state.last` is `None`, which is the shape 2026-09-02 never had
to handle and the most likely place for a build to raise.

---

## 7. Mutations to verify

1. Delete the `probe and` condition → T8 fails (the excluded sites start asking).
2. Change the default to `probe: bool = False` → T1–T3 fail.
3. Add `ABORTED` to the set → T6 fails.
4. Drop `state.last` reassignment on a positive probe → T1 fails on `filled_contracts`
   (this is the half-applied fix 2026-09-02 warned about; it must be killed by a test,
   not by review).
5. Move `state.sent` onto the filled order → T1 fails on `final_limit_price`.
6. Make a failed probe return `UNRESOLVED` instead of falling back → T5 fails.
7. Run the probe **before** `_request_cancel` on the `NEVER_WORKED` path → should NOT
   change any test. If it does, the ordering is load-bearing in a way §3.5 has not
   understood, and that is a finding.
8. Remove a member from `_NEVER_ASKS` without adding it to `_ASK_BEFORE_CONCLUDING` →
   T10 fails on the union. Add it to **both** → T10 fails on disjointness. Both halves
   must be asserted; one alone lets a member be silently double-classified or absent.
9. Drop the `mutation_error = None` line on a converting probe → should not change any
   test today (`NO_ORDER_EXISTS` only arrives with `probe=False`). If a test *does*
   fail, a call site is reaching the gate that §4.2 has not classified.

---

## 8. Deploy shape and risk

**`src/` only. No config key, no migration, no new audit category** — the image
deploys alone and joins no coupled deploy. By the v0.3.20 classifier this restarts the
trading process (it changes the image).

**The risk is the file, not the change.** `loop.py` is the walk state machine shared by
the entry and exit paths, and this edits its terminating frame. Every outcome except
`FILLED` passes through the modified code. Against that:

- The change can only ever convert a **non-fill verdict into a fill verdict**, and only
  when the broker positively names a FILLED order under the identifier. It cannot
  invent a fill, cannot suppress one, and cannot send anything.
- A position reported `FILLED` when it filled is the *less* dangerous error direction
  than today's: today a filled spread is reported unfilled, and on the exit path that
  spends its one attempt.
- Position safety never depended on this. `positions` is written from the fill feed by
  chunk 2, independently of the walk — which is why 2026-09-09's mis-reported spread
  was still adopted, watched and exited for +40.00.

**Rate is unknown and n=1.** 2026-09-02's version measured 1-of-3 on successful
replaces; this branch is narrower (it needs the replacement to stay unreadable or
non-`Live` for a full `working_timeout_s`), so that rate must not be assumed to carry.
`suppressed_outcome` (§3.6) is what turns this into a measurement instead of a second
guess.

**Timing.** This is a walk-loop change proposed while `execution.walk_source` /
`watchdog.trigger_source` still carry SANDBOX-ENTRY values and a second data-backed
pass is owed before size is scaled. It does not touch those parameters, but it is
worth one deliberate decision about whether to land it before or after the live flip
rather than letting the ordering happen by default.

---

## 9. Sign-off — RECORDED 2026-09-10

Three confirmations were put to Richard, each a place this proposal does more than the
finding required. **All three recommendations were taken; nothing was overruled.**

| # | Question | Decision |
|---|---|---|
| §3.1 | Gate at `_finish` (deleting `_resolve`'s call) vs patching the three branches | **`_finish` gate, `_resolve`'s call deleted.** Edits 2026-09-02's shipped code rather than adding beside it; the case rests on this being the third instance of one class, not on elegance |
| §3.2 | Are `WALKED_AWAY` and `DRIFTED` in the set | **In.** The offered cut was declined and so was the "skip it on the exit path" middle option — the rule stays uniform, and the redundant exit-side GET is paid deliberately |
| §3.3 | `probe=False` keyword vs a `_Walk` flag | **Explicit keyword, default `True`** |

Nothing in this document is open. A build session works from §4 (the change), §4.1 (the
shape), §4.2 (all seventeen call sites classified), §6 (tests) and §7 (mutations)
without re-deciding anything.

**On ship**, one commit, then:

- the decision record `2026-09-10-never-worked-is-not-a-verdict.md` updated from
  DIAGNOSED to BUILT, and its "Open design questions" section replaced by what was
  decided;
- its row in `docs/decisions/README.md` updated to match;
- a CLAUDE.md paragraph, carrying the two rules worth surviving this document: **the
  trigger for asking is that the WALK is about to conclude, not that the BROKER
  already did**, and **`_finish` is the only place the rule can be an invariant rather
  than a habit**;
- this file marked SHIPPED with its outcome — including any recommendation the build
  overturns on contact with the code, which is what this directory is for.

**Recheck at build time**, since both are properties of *other* code that this design
leans on: that `_RETRYABLE_EXITS` still holds only outcomes meaning "nothing filled"
(§3.7), and that `_after_walk` still routes every non-`WALKED_AWAY` outcome away from
the cross (§3.2). Neither is expected to have moved; both would change what is written
above if they had.

---

## 10. As built — 2026-09-10

`loop.py` only, plus one additive comment beside `watchdog.py`'s `_RETRYABLE_EXITS`
that §5 asked for. No config key, no migration, no new audit category. 2063 tests
before, 2076 after — thirteen new, since T11 is the existing 2026-09-02 regression
passing UNEDITED, which is the assertion it was asked to make.

### What the build overturned

**§3.2 called the exit-side `WALKED_AWAY` coverage "redundant, not wrong". It is
PRE-EMPTIVE, not redundant.** The market-crossing escalation opens with
`_await_settled`, which asks the same identifier-wide FILLED question — but the gate now
answers it a frame earlier, so `_after_walk` returns on `FILLED` and `_cross` is never
reached. The cancel/fill-cross case `_await_settled` reports as
`ExitOutcome.FILLED_LATE` is therefore now reported as plain `FILLED`
(`test_a_fill_that_crosses_the_cancel_is_reported_and_not_crossed`, which had to be
updated).

Nothing load-bearing moved: nothing is crossed, exactly one order is sent, and
`ExitOutcome.closed` is true either way. What is lost is a NAME — *the walk gave up and
the order filled anyway* — now carried at Tier 2 as `walk.filled_under_identifier` with
`suppressed_outcome: walked_away`, and one alert kind (`watchdog_exit_filled` rather
than `watchdog_exit_filled_late`; both page). `needs_attention` has no callers in
`src/`. **Not designed around**, because carving `WALKED_AWAY` out on the exit path is
exactly the third option §3.2 declined; restoring `FILLED_LATE` is a taxonomy change on
its own decision, in the taxonomy's own file.

### Where the build read §4.1 against §3.6

§4.1's illustrative block gates the three `terminal_*` fields on `state.last is not
None`. §3.6 requires them `None` on the deadline branches, where `terminal_status: null`
**is** the signature separating this defect from 2026-09-02's. Those differ: on the
`NEVER_WORKED` branch `state.last` is populated with a NON-terminal sighting (the ack at
`Received`), so the `is not None` form would emit `terminal_status: "Received"` and lose
the signature. Built to §3.6, via `_is_terminal_observation` — None-safe and
terminal-gated. §4.1 says it is illustrative and points at §3.6; that is how the
tension was read.

### §7 #7 — the ordering, and what isolating it found

**Prediction holds on the `NEVER_WORKED` path**: no test distinguishes probe-before-cancel
from probe-after. Two mechanical forms of the mutation were tried and each fails only on
`cancel_requested`, for its own mechanical reason — one builds the result before the cancel
runs, the other guards the relocated cancel on an outcome the probe has already
converted. Neither moves the probe's ANSWER: no outcome, `filled_contracts`, reason,
telemetry field or broker id changes.

**Found while isolating it: on the `WALKED_AWAY` path the ordering IS load-bearing, and
in the direction §3.5 predicted.** Hoisting the probe above `_request_cancel` there makes
`test_a_fill_that_crosses_the_cancel_is_reported_and_not_crossed` report `FILLED_LATE`
again — the probe runs before the fill that crosses with the cancel, misses it, and
`_await_settled` catches it a frame later. So *"probing last makes the answer as fresh as
the frame allows"* is now backed by a test rather than only asserted, and it is also the
exact mechanism of the pre-emption recorded above: the gate reaches `FILLED_LATE`'s case
first precisely BECAUSE it probes after the cancel.

### Two smaller notes

- **T8 as worded does not survive contact.** "`_recover`'s two finishes and `run()`'s
  `BrokerUnavailable` handler make exactly one `by_identifier` call between them" holds
  for the two `_recover` sites; the third depends on what raised `BrokerUnavailable` (a
  read-exhausted `_recover` makes three). Built as: each site's count is exactly what
  the SITE itself made, and the gate added none — which is the property.
- **§5's claim that a `FILLED` exit "never spends the attempt and never fires
  `_SPENT`" is not how `_run_exit` works**, and open question 4 of the finding asked for
  this to be confirmed rather than assumed. `FILLED` reaches the same terminal branch as
  every other outcome that got an order to the broker: `_attempted` is written and the
  `_SPENT` episode is armed. What does not happen is the PAGE — the closing fill books,
  the row leaves the ledger. The operative half of §5 ("never reaches `NOT_RESOLVED`")
  is correct and is what T12 pins.
