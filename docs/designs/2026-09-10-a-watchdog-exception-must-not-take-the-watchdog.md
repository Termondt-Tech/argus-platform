# A watchdog exception must not take the watchdog

**Status: BUILT 2026-09-10.** The deviations contact with the code forced are in
**"As built"** (§11) — two of them correct this document's own §4.2.

Finding: [go-live findings §3, Open decisions](../go-live-findings.md#open-decisions) —
*"A watchdog exception is still `HaltGrade.ALL` and still sets the shared stop event."*
Cost, measured: **≈ $670 on 2026-09-03**, against the same day's surviving-watchdog
sibling.

Precedents this stands on, all four load-bearing:
[halt scope split](../decisions/2026-08-29-halt-scope-split.md) (P3 — the grade
vocabulary), [end-day keeps the watchdog](../decisions/2026-09-01-end-day-keeps-the-watchdog.md)
(the same conclusion applied one level down, through a latch rather than a stop
event), [exit guard unreachable](../decisions/2026-09-03-exit-guard-unreachable.md)
(which removed the *cause* that fired and explicitly left this class open), and
[exit attempt taxonomy and halt scope](../decisions/2026-08-28-exit-attempt-taxonomy-and-halt-scope.md)
(*a monitor must not be disabled by the failure of an action it requested*).

> **Line numbers** are against `941353b` (`v0.3.22-10-g941353b`). All
> `src/argus/execution/watchdog.py` unless said otherwise.

---

## 1. Recommendation in one paragraph

**Grade the watchdog's halt `ENTRIES`, stop setting the shared stop event, and — the
half without which the first two make things *worse* — stop returning from `run()`.**
A watchdog exception becomes a *degradation* rather than a death: the kill switch is
engaged immediately (permanent, human-cleared, exactly as today), the process keeps
its scheduler, and the watchdog keeps evaluating, marking, closing and flattening on
its next cadence tick. The catch is scoped **per position** as well as per pass, so one
poisonous spread cannot blind its sibling — which is the shape the 2026-09-03 loss
actually took, one layer up. Three hazards that only exist once the watchdog survives
are closed in the same change: an exit task that raises must **spend the position's
attempt** (or the next tick sends a second closing order — the double-close the
one-attempt rule exists to prevent); `_reap_exits` must run in a `finally` (or an
aborted pass leaves a finished exit task in `_exiting` and that position is skipped for
the process lifetime); and `AuditWriteError` is the **one load-bearing exclusion**,
keeping today's `ALL` posture verbatim. `/status` gains a Watchdog row, because a
degraded watchdog that keeps running is by construction less visible than one that
parked the process.

**`src/` only. No new config key, no migration, no new audit *category* family beyond
three records.** The image deploys alone.

Rejected: grading the halt and changing nothing else (§3.2, option A) — it is the
smallest diff, it is what the finding literally asks for, and it produces a session
that runs to the close with a **dead watchdog and a healthy-looking process**. That is
strictly worse than today, where the hard stop at least prompted the restart that
recovered the position in eight seconds.

---

## 2. What is broken, in source

### 2.1 Three mechanisms, not one

`_halt_on_exception` (`3858`) is reached from two call sites — `run()`'s pass catch
(`2566`, `where="check"`) and `_run_exit`'s (`3511`, `where="exit"`) — and it takes the
watchdog down **three independent ways**:

| # | Line | Mechanism | What it destroys |
|---|---|---|---|
| 1 | `3881` | `self._stop.set()` | The **shared** session stop event. Ends the signal loop, aborts any exit walk in flight (`ABORTED`), and ends the session body. |
| 2 | `3898` | `await self._halt(...)` with **no `grade=`** → `HaltGrade.ALL` (`app.py:1437`) → `request_stop()` (`app.py:1493`) | The scheduler, and therefore every remaining session. The process parks. |
| 3 | `2567` | `return` | The watchdog task itself, for the rest of the day — **independent of both of the above.** |

The finding names 1 and 2. **3 is the one that has to be fixed for either to help**,
and it is not in the finding at all. Fixing 1 and 2 alone yields: session continues,
scheduler continues, `/status` reports `running`, no HALTED-`ALL` badge — and nothing
watching a held 0DTE position. The operator's cue to restart disappears along with the
protection.

### 2.2 The stop event is shared, and 2026-09-01 already wrote the rule

`stop` reaches the watchdog from `session._build_trading_side` (`session.py:772`,
`stop=stop`) and is the **same object** given to `ExitExecutor` (`session.py:756`) and
`EntryExecutor` (`session.py:824`), which is `session_stop`, which the scheduler bridges
from the process stop event (`scheduler.py:153`). So `self._stop.set()` at `3881` is not
"stop the watchdog"; it is "end the trading day, and abort the exit walk that is
currently trying to close the position this exception was raised about".

`_end_day` had exactly this defect and it was fixed on 2026-09-01 by *not using the
shared event*: `EntriesEnded` is a session-scoped frozen latch with one reader that
cannot be awaited, deliberately not a second stop event. The rule that record states —
**narrow the ACTION, never widen the event** — applies here unchanged.

### 2.3 The strict posture is self-defeating, in its own words

The module docstring (`61`) and `run()`'s (`2551`) both justify the halt as:

> the failure mode is a real position sitting open with nothing watching it, which is
> the precise gap this module was built to close.

The response to that failure mode is to **guarantee it**. On 2026-09-03 the halt
produced, for `call-7725-7730`: no virtual stop, no mark, no `/status` row, and no
15:30 `FLATTEN_BEFORE_CLOSE`, on a position that was priced at 1.65 the same second the
exception fired. The manual restart at 15:37 restored all of it in eight seconds under
the same broker conditions — which is the strongest available evidence that the halt
was never protecting anything a narrower grade would have exposed
([the 2026-09-03 record](../decisions/2026-09-03-exit-guard-unreachable.md), *"The halt
did not need to be process-wide"*).

### 2.4 What can actually raise — the transient class is already gone

This matters, because it decides whether the halt may fire on the *first* exception
(§3.3). Every broker round trip on the check path already swallows:

| Call | Line | Today |
|---|---|---|
| `_refresh_working_orders` → `get_live_orders()` | `3210` | swallows, Tier 2 `working_orders_unread` (52 in one cert session) |
| `_recheck_residual_orders` → `residuals.step` | `3233` | swallows, Tier 2 `residual_recheck_failed` |
| `_cache_is_over_reporting` → REST confirm | `2906` | **every failure path returns False** (pages, never raises) |

What is left that can reach `run()`'s catch on the check path:

- `AuditWriteError` from a Tier 1 `record` — which has **already** halted the process
  `ALL` through `AuditLog.on_failure` → `app._halt_on_audit_failure` (`app.py:561`,
  `1545`) before it propagates, and has already set `_stop` through that halt's
  `request_stop`;
- a **local sqlite** failure in `open_spreads` (`3135`) or `ledger_state` (`2843`) — and
  a sqlite failure that stops reads generally stops writes, i.e. becomes the row above;
- a **genuine defect** — `AttributeError`/`TypeError`/`KeyError` in evaluation, payload
  construction, or scan attribution.

So on the check path there is **no known transient left**. On the exit path there is:
`ExitExecutor.close` is a large surface that does touch the broker, and the one
observed instance in the system's life came from exactly there.

### 2.5 Two hazards that only exist once the watchdog survives

Both are created by this change and must be closed inside it.

**(a) A crashed exit task never spends the attempt.** `_run_exit` (`3499`) writes
`self._attempted[key]` only on the normal return path (`3608`). On the `except` branch
(`3508`) it halts and returns — `_attempted` unwritten, `_exiting[key]` reaped on the
next `_reap_exits`. Today that is harmless because the process stops. **Under this
design the next cadence tick re-evaluates the position, the trigger is still sustained,
and `_trigger` hands off a second closing order** — for a position whose first closing
order may or may not have reached the broker, because an exception is precisely the
state in which we do not know. That is the double-close `_attempted` exists to prevent,
and it is worse than any case the one-attempt rule was written for.

**(b) An aborted pass never reaps.** `self._reap_exits()` (`2664`) is the last statement
of `check_once`. A pass that raises anywhere above it — today, once, then the process
halts — leaves finished exit tasks in `_exiting`. `check_once` skips any key in
`_exiting` (`2602`) as "an exit is in flight". Under this design that is a **permanent,
silent** removal of that position from the watch list, with `_impaired` popped on every
tick so `/status`'s evaluability row goes quiet too.

---

## 3. The design questions, resolved

Every question below is answered here. Nothing in §4 depends on an answer that is not
in this section.

### 3.1 (Q1) The grade — **`ENTRIES`, and it is the verdict's own property**

`HaltGrade.ENTRIES` is exactly the right shape and it already exists: the kill switch is
still engaged, the halt is still permanent, `/status` still renders HALTED, the process
still will not enter again without a human — and the scheduler runs, so the day's
session comes up monitor-only with the watchdog watching, marking, closing and
flattening (`app.py:1046`, `session.py:1435`).

The grade is passed by the watchdog, not by the wiring seam, because **P3's own rule is
that the grade is a property of the failure and not of the call site** (`app.py:708`:
*"How wide it parks is the verdict's own property … not this call site's"*). `HaltGrade`
is imported into `watchdog.py` from `argus.reconcile`.

**No cycle**: `reconcile.py` imports `argus.execution.models` and
`argus.execution.store` (`reconcile.py:87`, `96`), neither of which imports
`watchdog.py`. Moving `HaltGrade` to a new `argus/halt.py` would be tidier layering and
is **rejected as churn** — it touches `app.py`, `session.py`, `status.py`,
`control.py`, `reconcile.py` and their tests for no behavioural gain. If a second
`execution/` module ever needs the enum, that move becomes right.

**`HaltGrade.tighten` (`reconcile.py:1162`) makes this safe for free.** The grade only
ever tightens (`app.py:1519`), so a watchdog `ENTRIES` halt arriving after an `ALL` halt
cannot relax it — which is the whole of why `AuditWriteError` needs no grade logic in
this module at all (§3.5).

### 3.2 (Q2) Does the loop survive? — **yes, and it is the load-bearing half**

Three options were considered:

- **A. Grade only** (`return` kept). Rejected in §1: the watchdog is dead for the
  session, the process looks healthy, and the operator's cue to restart is gone. The
  finding as literally written asks for A; A is a regression.
- **B. Restart the watchdog task on failure.** Rejected: the state that must survive a
  failure is exactly the state a restart destroys — `_attempted`, `_flatten_attempted`,
  `_sends`, `_incoherent_credit_paged`. A restart that preserved them would be
  indistinguishable from continuing the loop, and one that did not would hand every
  position a fresh exit budget on a crash. **Continuing IS the restart, minus the way to
  get it wrong.**
- **C. Continue the loop.** Taken.

### 3.3 (Q3) When does the halt fire — first exception, or a bound? — **first, unchanged**

The codebase's settled split is: *a failure to REACH the broker gets a bound; a broken
invariant halts immediately.* The bounded-retry precedents (reconcile broker-unreachable
2026-08-26, entry guard 2026-08-26, exit guard 2026-09-03) are all broker-reachability
questions. §2.4 shows that **every broker round trip on the check path already
swallows**, so what is left there is not the class those bounds were built for.

A bound was still considered, and rejected on three grounds:

1. It needs a number — a new config key (a coupled deploy, which this change otherwise
   avoids entirely) or a fourth magic constant.
2. Its false-negative window is entries taken while blind. At `cadence_s` a
   three-pass bound is ~15 s in which `PreTradeGate` can authorise a position that
   nothing will be able to stop out.
3. It buys nothing on the cost side. **Today's posture already trips the kill switch on
   the first exception, and since 2026-09-01 that trip is DURABLE** — it survives the
   restart and needs `POST /resume?confirm=yes`. A bound does not reduce that cost; it
   only delays it. Nothing regresses by keeping the trip immediate, and everything that
   is being fixed here is *what else* the trip took with it.

**So: immediate, exactly as today. One grade narrower, and the watchdog lives.**

### 3.4 (Q4) Per-position isolation — **yes**

Without it, a defect in one spread's evaluation aborts the pass before the second
spread is reached, on every tick, forever — `_scan.spreads` ordering is stable. For a
two-position book that is the 2026-09-03 harm reproduced by a different route, and it is
the *routine* case: two same-underlying, same-day spreads, one of them carrying the odd
field.

The catch goes around the loop body, extracted to `_check_one(spread,
seconds_to_flatten)`. It does **not** spend anything: nothing was sent, so the position
keeps both allowances and the trigger stays latched — the same rule `NOT_ATTEMPTED`
already follows.

`_refresh_if_due` gets its own catch, separate from the loop, for a reason worth
stating: `_last_refresh` is set **before** the await (`3130`), so a failed refresh is not
retried until the next interval — and the evaluation loop must not be skipped for that
interval just because the rebuild failed. One failed rebuild costs freshness, not
watching.

### 3.5 (Q5) `AuditWriteError` — **the one load-bearing exclusion, keeping `ALL` verbatim**

Same shape as `_ask_guard`'s exclusion (2026-09-03) and for a stronger reason: by the
time it reaches this module, `AuditLog._fail` has already fired `on_failure` →
`_halt_process(grade=ALL)` → `request_stop()` → the shared stop event is already set.
Continuing would loop exactly once more and exit on `while not self._stop.is_set()`.

So it takes today's path unchanged — `_halt_on_exception(exc, where=...)`, which keeps
`self._stop.set()` and the default `ALL` — and `run()` returns. **One typed exclusion,
stated, and no other exception type is load-bearing anywhere in this design.** The
non-negotiable ("a failed audit write halts trading") is untouched.

`_degrade`'s own Tier 1 write is wrapped in `contextlib.suppress(Exception)`, on the
same reasoning `_halt_on_exception` already gives at `3896` — the recorder may be
exactly what failed, and the halt must not depend on it.

### 3.6 (Q6) A crashed exit spends the attempt — **as `NOT_RESOLVED`, with its own sentence**

Per §2.5(a), it must spend. Which value?

**The verdict is `NOT_RESOLVED`, and no new `ExitOutcome` member is added.** The
question `ExitOutcome` answers for the retry machinery is *may this position get another
automated send?* — and for a crash the answer is the absolute NO that `NOT_RESOLVED`,
`BLOCKED` and `CANCELLED_ELSEWHERE` already carry. Adding a member would force a
classification decision in `_RETRYABLE_EXITS`, `outcome.closed`, and every reader, for a
distinction none of them acts on. This is the 2026-09-03 rule verbatim: *a fourth
sentence, deliberately not a fourth outcome.*

**The sentence is a new field**, because the surfaces that render the verdict would
otherwise tell an operator the *broker* did something. `_SpentAttempt` (`1990`) already
carries two such sentences with disjoint contracts — `reject_reason` (an order the
broker accepted then rejected) and `mutation_error` (one it would not accept at all).
A crash is neither, and overloading either breaks the AST rule that **nothing decides on
them** (2026-09-04). So: `crash_error: str | None = None`, third of three, same
`None`-not-`""` discipline.

It reaches `/status`. `SpentExitAttempt` (`1789`) and `UnclosablePosition`
(`status.py:311`) gain the field, because that row's entire job is telling a human which
*kind* of manual intervention this is, and `not_resolved` with no broker reason anywhere
is the reassuring-when-wrong reading this page has been fixed for twice already.

**What is NOT restored**: `_flatten_attempted`. It is claimed at the handoff (`3488`) as
the fail-closed default, and 2026-09-02's rule is exact — *a crash is an EXCEPTION: it
never reaches the restore, and leaves the allowance spent. A crash must not be able to
GRANT; only a positive value may RESTORE.* A crash during the ordinary attempt leaves
the flatten override untouched and available, which is the correct and useful case: at
15:30 that position still gets **one** further send, through `DuplicateOrderGuard`,
which re-asks the broker and permits only DEAD. **The design needs no retry of its own
because P2's override already is one, and it is confirmation-gated.**

### 3.7 (Q7) Does a degraded watchdog stop SENDING? — **no, and here is the risk it accepts**

The one way a surviving watchdog can lose money that a dead one cannot: if
`open_spreads` (`3135`) fails repeatedly, `_scan` goes stale, and the watchdog could
trigger an exit on a spread that has since closed — sending a closing order for legs it
does not hold, which opens the inverted vertical.

Gating sends on scan freshness was considered and **rejected**:

- The failure mode requires the ledger *read* to keep failing while the audit log keeps
  *writing* (a read failure that is really a sqlite failure becomes `AuditWriteError`
  and takes §3.5's path). That is a compounding, unobserved failure.
- The harm it would prevent is bounded by the spread width and requires that
  compounding. The harm it would *cause* is certain and is this document's subject: a
  0DTE position that cannot be flattened at 15:30 because the ledger read was flaking.
- Suppressing the send because a read failed is the same self-defeating move as
  halting the watchdog because the watchdog failed — 2026-08-28's rule one level down:
  *a monitor must not be disabled by the failure of an action it requested.*

**Recorded, not silenced:** `watchdog.exit_started` gains `scan_age_s` (seconds since
the last successful `open_spreads`), so the one number that would evidence this is in
the trail from day one, and the `/status` Watchdog row carries "last clean pass Ns ago".
Nothing decides on either.

### 3.8 (Q8) Config — **none. A module constant.**

No new key, so **the image deploys alone** — the property four of the last six releases
did not have. The only number is the degraded episode's repeat-page cadence:

```python
#: How often a degraded episode re-pages, via `_Escalation.overdue`. A CONSTANT, not a
#: config key: a new REQUIRED key forces a coupled config+image deploy, and this is a
#: paging cadence, not a rail. 300s puts ~3 repeats inside `SlackNotifier`'s 900s
#: window, where they coalesce into one `(+N suppressed)` message — the bounded-RATE
#: shape `_Escalation.overdue` documents, never a single page.
_DEGRADED_PAGE_INTERVAL_S = 300.0
```

Reusing `watchdog.exit_unattempted_max_s` was rejected: it is the bound on *one
position* having no automated path to close, and widening its meaning silently is how a
tuned number stops meaning anything.

### 3.9 (Q9) `/status` — **a Watchdog row, and it is not optional**

A watchdog that parks the process is loud. A watchdog that keeps running while failing
every tick is, by construction, the quietest failure this system can produce: `phase:
running`, sessions scheduled, the 15-second status line printing. The HALTED row will
say `NO ENTRIES — watchdog still running`, which after this change is **the one
sentence that may be false**.

Placement: between **Open positions** (`status.py:1488`) and **Unwatched positions**
(`1497`). That block's own comment calls itself *"one escalating story about the same
held spreads — what is held, is it watched at all, can its triggers be evaluated, can it
be exited"*; "is anything being watched" is the step before all of them. It is not put
above HALTED: HALTED is the stronger claim and owns the "changes the meaning of
everything below" slot.

Three states:

| State | Badge | Cell |
|---|---|---|
| never failed | `ok` **WATCHING** | `N position(s), last pass Ns ago` |
| currently degraded | `bad` **DEGRADED** | `where`, `error_type`, `error`, `failing for Ns`, `last clean pass Ns ago`, affected identifiers, and the sentence *"the rows below are as of the last clean pass"* |
| recovered, halt still in force | `warn` **RECOVERED** | `N failure(s), last <error_type> at ...` + *"entries remain halted — POST /resume once resolved"* |

The third state is why the row exists at all: the halt is permanent and the failure is
not, so without it the operator sees a healthy watchdog and a HALTED badge whose reason
string is the only surviving trace of why.

### 3.10 (Q10) `fatal` is renamed

`self._fatal` (`2267`) and the `fatal` property (`2373`) become
`self._failure: _WatchdogFailure | None` and `last_failure`. A property named `fatal` on
an object that survives it is a name that lies, and it is read only by tests.

---

## 4. The change, file by file

### 4.1 `execution/escalation.py` — one additive read

```python
def active(self, key: str, tag: str) -> float | None:
    """When this (key, tag) episode started, or None if it is not active.

    Read-only, added 2026-09-10 so a caller that needs BOTH the edges and the live
    state does not have to keep a second copy of `_on` in sync with `rising`/
    `falling` — which is the drift `/status` has already been fixed for twice.
    """
    return self._on.get((key, tag))
```

No behaviour change; `residual_orders.py` shares this class and is unaffected.

### 4.2 `execution/watchdog.py`

**Imports.** `from argus.audit.log import AuditLog, AuditWriteError`;
`from argus.reconcile import HaltGrade`.

**New tag and key** (beside `_BOOK_KEY` at `1669`). Note `_DEGRADED` at `1617` is
**already taken** by the evaluability severity — the new tag must not reuse it:

```python
#: The WATCHDOG ITSELF is failing — a distinct condition from `_DEGRADED` above,
#: which is one position's trigger being unevaluable.
_WATCHDOG_FAILING = "watchdog_failing"
_WATCHDOG_KEY = "__watchdog__"
```

**New frozen record**, beside `_SpentAttempt`:

```python
@dataclass(frozen=True)
class _WatchdogFailure:
    where: str                      # "refresh" | "position" | "check" | "exit"
    error_type: str
    error: str                      # str(exc)[:500]
    at: float                       # the watchdog's monotonic clock
    external_identifier: str | None # the position, when the failure was scoped to one
```

**New live-read row** (the `/status` source, beside `SpentExitAttempt` at `1789`):

```python
@dataclass(frozen=True)
class WatchdogHealth:
    degraded: bool
    failures: int                       # process-lifetime total
    where: str | None                   # of the most recent failure
    error_type: str | None
    error: str | None
    degraded_for_s: float | None        # None unless currently degraded
    last_clean_pass_s: float | None     # None if no pass has ever completed clean
    affected: tuple[str, ...]           # identifiers that failed in THIS episode
```

**`_SpentAttempt`** gains `crash_error: str | None = None` (third field, after
`mutation_error`).

**`SpentExitAttempt`** gains `crash_error: str | None = None`.

**State** in `__init__`: `_failure`, `_failures` (total), `_pass_failures` (this pass),
`_last_clean_pass: float | None`, `_affected: set[str]`, `_last_scan_ok: float | None`.
`_fatal` is removed.

**`run()`** (`2549`):

```python
while not self._stop.is_set():
    self._pass_failures = 0
    try:
        await self.check_once()
    except asyncio.CancelledError:
        raise
    except AuditWriteError as exc:
        # THE exclusion (§3.5). The recorder is dead process-wide, `on_failure`
        # has already halted ALL and set this very event, and there is nothing
        # left for a watchdog to protect. Today's path, verbatim.
        await self._halt_on_exception(exc, where="check")
        return
    except Exception as exc:
        # The backstop. `check_once` isolates the refresh and each position, so
        # reaching here means the pass frame itself failed (`_seconds_to_flatten`,
        # the iteration). Degrades; does not return.
        await self._degrade("check", exc)
    except BaseException as exc:
        # SystemExit / KeyboardInterrupt / GeneratorExit: the interpreter is
        # leaving. Deliberately NOT halted — the kill switch is DURABLE since
        # 2026-09-01, and tripping it on the way out would cost a human a
        # `POST /resume` on the next boot for a clean shutdown.
        logger.critical("position watchdog: %r — not a halt", exc, exc_info=exc)
        self._notify("watchdog_fatal", {"where": "check", "error_type": type(exc).__name__})
        raise
    await self._settle_pass()
    await self._interruptible_sleep(self._wd.cadence_s)
```

**`check_once()`** (`2570`) — three structural edits, no logic moved:

```python
try:
    await self._refresh_if_due()
except asyncio.CancelledError:
    raise
except AuditWriteError:
    raise
except Exception as exc:
    # `_last_refresh` was already advanced at 3130, so this costs freshness for
    # one interval, not the evaluation pass.
    await self._degrade("refresh", exc)
try:
    seconds_to_flatten = self._seconds_to_flatten()
    for spread in self._scan.spreads:
        try:
            await self._check_one(spread, seconds_to_flatten)
        except asyncio.CancelledError:
            raise
        except AuditWriteError:
            raise
        except Exception as exc:
            await self._degrade("position", exc, spread=spread)
finally:
    # In a `finally` since 2026-09-10 (§2.5b). A pass that aborts above this
    # leaves a FINISHED exit task in `_exiting`, and `check_once` skips any key in
    # `_exiting` — so the position is dropped from the watch list permanently and
    # silently, with `_impaired` popped every tick so even the evaluability row
    # goes quiet. Synchronous, touches no I/O, and cannot itself fail.
    self._reap_exits()
```

`_check_one` is the existing loop body (`2601`–`2663`) extracted verbatim.

**`_refresh_if_due`** (`3120`): `self._last_scan_ok = now` immediately after
`self._scan = scan` (`3151`) — i.e. only on success.

**`_trigger`** (`3395`): `watchdog.exit_started`'s payload gains
`"scan_age_s": None if self._last_scan_ok is None else round(self._clock() - self._last_scan_ok, 3)`.
(It is built in `_run_exit`'s executor call chain; the record at `3155` is the natural
home — see §6 T13.)

**`_run_exit`** (`3499`) — the except branch becomes:

```python
except asyncio.CancelledError:
    raise
except BaseException as exc:
    # IN-MEMORY SAFETY STATE FIRST, before anything that can fail — the same
    # ordering rule `KillSwitch.engage` follows. An exception is the one state in
    # which we do not know whether an order reached the broker, so it is the
    # STRONGEST case for the one-attempt rule, not an exception to it.
    self._spend_on_crash(key, exc)
    if isinstance(exc, AuditWriteError):
        await self._halt_on_exception(exc, where="exit")
        return
    if not isinstance(exc, Exception):
        raise
    await self._note_exit_crash(spread, exc)
    return
```

**New methods:**

```python
def _spend_on_crash(self, key: str, exc: BaseException) -> None:
    """Spend the attempt. In-memory only; cannot fail; called before anything else."""
    self._attempted[key] = _SpentAttempt(
        ExitOutcome.NOT_RESOLVED,
        self._clock(),
        reject_reason=None,
        mutation_error=None,
        crash_error=f"{type(exc).__name__}: {exc}"[:500],
    )
    self._escalation.rising(key, _SPENT)      # arm the repeating page, as `_run_exit` does
    self._escalation.falling(key, _UNATTEMPTED)
    for tag, _c, _k in _RETRYABLE_EXITS.values():
        self._escalation.falling(key, tag)

async def _note_exit_crash(self, spread, exc) -> None:
    """Tier 1 + page + halt ENTRIES. One-shot per position; the SPENT episode repeats."""
    detail = {
        "external_identifier": spread.external_identifier,   # top level: alert_scope (B1)
        "candidate_id": spread.candidate_id,
        "contracts": spread.contracts,
        **spread.credit_payload(),
        "outcome": str(ExitOutcome.NOT_RESOLVED),
        # Both None, and carried LITERALLY rather than omitted: the AST sweep
        # (tests/test_watchdog_exit.py:1490) governs any alert naming an outcome, and
        # "no broker was involved" is the truthful answer, not a missing field.
        "reject_reason": None,
        "mutation_error": None,
        "crash_error": f"{type(exc).__name__}: {exc}"[:500],
        "error_type": type(exc).__name__,
    }
    logger.critical(...)
    with contextlib.suppress(Exception):
        await self._audit.record("watchdog.exit_crashed", detail)
    self._notify("watchdog_exit_crashed", detail)
    self._record_failure("exit", exc, spread.external_identifier)
    await self._halt_entries("exit", detail)

def _record_failure(self, where, exc, key=None) -> None:
    """THE only writer of `_failure`/`_failures`/`_affected`. In-memory, cannot fail."""

async def _degrade(self, where, exc, *, spread=None) -> None:
    """Record the failure and count it against THIS pass. Edges are `_settle_pass`'s."""
    self._record_failure(where, exc, spread.external_identifier if spread else None)
    self._pass_failures += 1
    logger.error(...)

async def _settle_pass(self) -> None:
    """The episode's edges, once per pass. The ONE place the degradation halts."""
    if self._pass_failures:
        detail = {...}   # where/error_type/error/failures/affected/last_clean_pass_s
        if self._escalation.rising(_WATCHDOG_KEY, _WATCHDOG_FAILING):
            with contextlib.suppress(Exception):
                await self._audit.record("watchdog.degraded", detail)
            self._notify("watchdog_degraded", detail)
            await self._halt_entries("check", detail)
        else:
            self._audit.telemetry("watchdog.degraded_still", detail)
            if (for_s := self._escalation.overdue(
                _WATCHDOG_KEY, _WATCHDOG_FAILING, _DEGRADED_PAGE_INTERVAL_S)) is not None:
                self._notify("watchdog_degraded", {**detail, "degraded_for_s": round(for_s, 1)})
        return
    self._last_clean_pass = self._clock()
    if self._escalation.falling(_WATCHDOG_KEY, _WATCHDOG_FAILING):
        detail = {...}
        self._affected.clear()
        with contextlib.suppress(Exception):
            await self._audit.record("watchdog.recovered", detail)
        # Entries stay HALTED — the halt is permanent by design. The page says so,
        # because a recovered watchdog with a HALTED badge is otherwise unexplainable.
        self._notify("watchdog_recovered", detail)

async def _halt_entries(self, where: str, detail: dict) -> None:
    """The narrow halt. `_halt_process` is idempotent, so repeats cost nothing."""
    await self._halt(
        f"position watchdog failed in {where} "
        f"({self._failure.error_type}: {self._failure.error})",
        source=HALT_SOURCE,
        grade=HaltGrade.ENTRIES,
        detail=detail,
    )

def watchdog_health(self) -> WatchdogHealth:
    """Live read for `/status`. In-memory only — no try/except, deliberately."""
```

**`_halt_on_exception`** (`3858`) survives **unchanged in body**, reached only by the two
exclusions. Its docstring is rewritten to say so, and `self._fatal = exc` becomes
`self._record_failure(where, exc)`.

**`_page_if_spent_and_still_held`** (`3716`) and **`_flatten_may_act`** (`2684`): both
build their detail from `_SpentAttempt` and both gain `"crash_error": spent.crash_error`
beside the two existing sentences.

**Module docstring** (`61`) and **`run()`'s** (`2551`): rewritten. The new statement of
policy:

> **On exception policy.** An unhandled exception in this task engages the kill switch
> at `HaltGrade.ENTRIES` and **keeps watching**. The old posture — halt `ALL`, set the
> shared stop event, return — was signed off on the reasoning that a crashed iteration
> leaves a real position with nobody watching it, and it *produced* that outcome rather
> than preventing it: on 2026-09-03 it cost ≈$670 on one cert 502. Entries stop
> immediately and permanently, because a watchdog that raised cannot be trusted to
> stop out what a new entry would open; everything else runs on, because the watchdog
> is the only thing that can close or flatten what is already held. `AuditWriteError`
> is the one exclusion and keeps the old posture verbatim.

### 4.3 `status.py`

- `WatchdogHealth` frozen dataclass (the status-layer twin — same three-state shape,
  fields as §4.2, defined here so the module keeps its one-way dependency, exactly as
  `UnclosablePosition` is);
- `UnclosablePosition` gains `crash_error: str | None = None`;
- `StatusSnapshot.watchdog_health: WatchdogHealth | None`, into `to_payload()` under
  `"watchdog"`;
- `_watchdog_cell(...)` and the row at `1489` (§3.9);
- `_spent_exit_cell` renders `crash_error` when present.

### 4.4 `session.py`

- module-level `_to_watchdog_health(WatchdogHealth) -> status.WatchdogHealth`, in the
  seam block with the other five;
- the `if trading is not None:` block (`1531`) gains
  `def watchdog_health(wd: PositionWatchdog = trading.watchdog): return _to_watchdog_health(wd.watchdog_health())`,
  passed to `status.session_running(...)`. `None` in shadow, like the rest.

### 4.5 `tests/test_session_status_seams.py`

`_SEAMS` (`251`) gains `"_to_watchdog_health": (wd.WatchdogHealth, status.WatchdogHealth)`.
**`test_the_sweep_covers_every_converter_in_the_seam_block` fails until it does** — which
is the mechanism working, and is called out in that test's own docstring.

---

## 5. What is deliberately NOT changed

- **The one-attempt rule.** A crash spends; it does not retry. The confirmation-gated
  exit re-attempt stays an open decision, and this is not a step toward it.
- **`stop()` (`2275`) still sets the shared event.** It is the session's teardown path
  and the sharing is correct there — `ExitExecutor` must see the day end.
- **The kill switch's blast radius.** Still entry-only, still not passed to
  `ExitExecutor` (`session.py:731`). Nothing here lets a halt cancel a closing order.
- **`_refresh_working_orders` / `_recheck_residual_orders` keep swallowing.** They
  already answer this question correctly for their own reads and must not be folded
  into `_degrade`, which halts.
- **No new stop event and no new latch.** P3 rejected a second stop event and
  2026-09-01 rejected it again; this narrows an existing halt's grade instead.
- **`HaltGrade` stays in `reconcile.py`** (§3.1).
- **The AST alert sweep keeps its two fields.** Extending it to require `crash_error`
  would break `entry.py`'s sites, which splat a `WalkResult` — and a crash is a
  watchdog-level fact, not a walk-level one. The new page carries the field because
  §4.2 writes it, and `/status` carries it because the seam sweep forces it.

---

## 6. Tests

`tests/test_watchdog.py` unless said otherwise.

**Two existing tests are replaced, not deleted** — the load-bearing assertions survive:

- `test_an_unhandled_exception_halts_the_process` (`2899`) → **`..._halts_entries_and_keeps_watching`.**
  It currently does `await watchdog.run()` and relies on the function *returning*; under
  this design `run()` loops, so it drives `check_once` directly and asserts the halt via
  the injected seam. Surviving assertions: exactly one halt, `source == "watchdog_exception"`,
  the cause in the reason.
- `test_an_exception_inside_an_exit_halts_the_process_too` (`2938`) →
  **`..._spends_the_attempt_and_halts_entries`.** Surviving assertion:
  `len(halts) == 1`, `detail["where"] == "exit"`.

Every `halt` fake in the file (`288`, `2895`, `2912`) takes `grade=None` — mechanical.

| # | Test | Asserts |
|---|---|---|
| T1 | `a_failing_pass_does_not_set_the_shared_stop_event` | `stop.is_set()` is False after three failing passes |
| T2 | `a_failing_pass_halts_at_ENTRIES_never_ALL` | the halt seam receives `grade=HaltGrade.ENTRIES` |
| T3 | `the_loop_keeps_running_after_a_failing_pass` | `open_spreads` fails once then succeeds; the next pass evaluates and triggers normally |
| T4 | `one_poisonous_position_does_not_blind_its_sibling` | two spreads, the FIRST raising in `evaluate_triggers`; the second is evaluated, triggered and exited on the same pass |
| T5 | `the_halt_fires_once_per_episode_not_once_per_tick` | ten failing passes → one `watchdog.degraded` Tier 1 record, one halt call, one page |
| T6 | `a_degraded_episode_repages_on_the_bound` | clock advanced past `_DEGRADED_PAGE_INTERVAL_S` twice → three pages total, `degraded_for_s` rising |
| T7 | `recovery_records_and_pages_and_leaves_entries_halted` | a clean pass after failures → `watchdog.recovered`, `watchdog_recovered`, and the halt is NOT reversed |
| T8 | `AuditWriteError_keeps_the_old_posture` | shared stop IS set, no `grade=` (or `ALL`), `run()` returns |
| T9 | `a_dead_audit_log_still_halts` (`2963`, kept) | unchanged |
| T10 | `a_crashed_exit_spends_the_attempt` | after the crash, the next tick evaluates and **sends nothing**; `executor.calls == 1` |
| T11 | `a_crashed_exit_keeps_its_flatten_override` | past the flatten mark, the crashed position gets exactly ONE further send |
| T12 | `an_aborted_pass_still_reaps` | `_refresh_if_due` raises while a finished exit task sits in `_exiting`; the key is gone after the pass and the position is evaluated on the next one |
| T13 | `exit_started_carries_the_scan_age` (`test_watchdog_exit.py`) | `scan_age_s` present, and `None` before any successful scan |
| T14 | `the_crash_sentence_reaches_the_spent_page` | `_page_if_spent_and_still_held`'s detail carries `crash_error` |
| T15 | `the_crash_page_satisfies_the_outcome_sweep` (`test_watchdog_exit.py:1490`) | `watchdog.py:_note_exit_crash` appears in `checked` |
| T16 | `the_status_row_names_a_degraded_watchdog` (`test_session_status_seams.py`) | a real `PositionWatchdog` driven to failure renders DEGRADED in the HTML, with the error type |
| T17 | `the_status_row_says_recovered_while_still_halted` | the amber third state, end to end |
| T18 | `escalation_active_reports_the_episode_start` (`test_watchdog.py`) | `_Escalation.active` returns the `rising` instant and `None` after `falling` |

---

## 7. Mutations to verify

Each must be caught by a named test.

| # | Mutation | Caught by |
|---|---|---|
| 1 | `grade=HaltGrade.ENTRIES` → omitted (defaults `ALL`) | T2 |
| 2 | `self._stop.set()` re-added to the degrade path | T1 |
| 3 | `return` re-added after `_degrade` in `run()` | T3 |
| 4 | the per-position catch removed (pass-level only) | T4 |
| 5 | `_spend_on_crash` removed from `_run_exit`'s except | T10 |
| 6 | `_spend_on_crash` moved BELOW `_note_exit_crash` (so an audit failure skips it) | T10, with a `record` that raises |
| 7 | `_flatten_attempted.discard(key)` added to the crash path | T11 |
| 8 | `_reap_exits()` moved back out of the `finally` | T12 |
| 9 | `AuditWriteError` folded into the general `Exception` branch | T8 |
| 10 | `_halt_entries` called from `_degrade` instead of `_settle_pass` (per tick) | T5 |
| 11 | `overdue` replaced by a one-shot page | T6 |
| 12 | `_affected` not cleared on recovery | T7 |
| 13 | `crash_error` dropped from `_to_unclosable` | the seam sweep (`test_session_status_seams.py:282`) |
| 14 | `_to_watchdog_health` added without joining `_SEAMS` | `test_the_sweep_covers_every_converter_in_the_seam_block` |
| 15 | `reject_reason`/`mutation_error` omitted from the crash page | T15 / the AST sweep |

---

## 8. Deploy shape and risk

**`src/` only. No config key, no migration, no compose or unit change** — so
`argus-deploy.sh`'s classifier sees an image-only release, and the config bind mounts are
untouched. Same class as v0.3.15 and v0.3.16.

**Three new audit categories**, all in the existing `watchdog.*` family:
`watchdog.degraded` (Tier 1, edge), `watchdog.degraded_still` (Tier 2),
`watchdog.recovered` (Tier 1, edge), `watchdog.exit_crashed` (Tier 1, one-shot per
position). `watchdog.fatal` survives for the `AuditWriteError` path. Four new alert
kinds: `watchdog_degraded`, `watchdog_recovered`, `watchdog_exit_crashed`,
`watchdog_fatal` (kept). All per-position alerts carry `external_identifier` and
`candidate_id` at the TOP level for `notify.alert_scope` (B1).

**The residual risk this change accepts**, stated once so it is not discovered later:

- §3.7's stale scan. Recorded as `scan_age_s`, decided against gating.
- A defect that fails **every** pass now runs to the close instead of parking the
  process. That is the intent — entries are halted, exits and the flatten still try
  every tick — but it means the operator's cue is the `/status` row and the Slack page
  rather than a dead process. §3.9 exists for exactly this.
- **A `HaltGrade.ENTRIES` halt does not restart the process, and it never expires.**
  If the underlying cause is one a restart would clear (the 2026-09-03 case), the
  operator now has to choose to restart rather than having it chosen for them. The
  runbook note in §9 is the whole mitigation.

**Rollback** is the image tag; nothing in this release is config, so
`argus-deploy.sh <previous tag>` is complete.

---

## 9. Operating note for `README.md` / the runbook

One paragraph, because the operator's job changes:

> **A watchdog exception no longer stops the process.** `/status`'s Watchdog row is the
> place to look: `DEGRADED` (red) means the watchdog is failing right now and the rows
> below it are as of the last clean pass; `RECOVERED` (amber) means it is watching again
> but entries stay halted until `POST /resume?confirm=yes`. Either way the kill switch is
> engaged and the HALTED row reads `NO ENTRIES — watchdog still running`. **Deciding
> whether to restart is now yours** — a transient (a broker blip, a one-off read failure)
> is already recovered and a restart buys nothing; a defect that repeats every tick will
> repeat after the restart too. The one thing a restart does buy is a *clean* watchdog
> state, which matters if a position's exit attempt was spent by a crash: `/status`'s
> Exit attempts row names those, with `crash_error`.

---

## 10. Sign-off

Nothing in this design is left open for Richard. The ten questions in §3 are each
answered with the reasoning that decided them, and §5 lists what was deliberately left
alone. Two items are for **awareness rather than decision**:

1. **The false-positive cost is a durable kill-switch trip.** It already is today
   (2026-09-01 made the switch survive restarts), so nothing regresses — but under this
   design the process keeps running and looks healthy, so the `/status` row and the
   Slack page are the only cues. §3.9 and §9 are the mitigation, and they are the reason
   the row is not optional.
2. **This closes the class, not the open exit-re-attempt decision.** A crashed exit
   spends the position's attempt and gets exactly one further send at the 15:30 flatten,
   through `DuplicateOrderGuard`. Whether a *confirmation-gated* re-attempt should exist
   is untouched and stays open.

If anything material turns up on contact with the code, it lands in an "As built"
section here, in the shape the 2026-09-10 `_finish` design used.

---

## 11. As built

Everything in §4 landed as written except the items below. The full suite — 2113
tests — passes with the change; the two replaced tests carry their surviving
assertions, and T1–T18 are all present. Mutations 1, 2, 5 and 8 were applied
and caught by their named tests during the build; the rest are covered
structurally (the seam sweeps) or by the assertions the table names.

**Two corrections to this document's own §4.2, both forced by its own test table:**

1. **The `_reap_exits` `finally` wraps the WHOLE pass, refresh included.** §4.2's
   `check_once` sketch wrapped only the evaluation loop, leaving the refresh's
   catch outside — so an aborted refresh (the `AuditWriteError` path) skipped the
   reap, which is precisely the §2.5(b) harm the `finally` exists to close. T12
   drives a refresh-abort with a finished exit task in `_exiting` and failed
   against the sketch; the `finally` now covers the pass frame.
2. **`_pass_failures` is reset at the top of `check_once`, not by `run()`.** The
   design's `run()` resets it before each `await check_once()` — but the design's
   own tests (T5, T7, and both replacements) drive `check_once` directly, and a
   `run()`-owned reset would make every directly-driven pass inherit the previous
   pass's failure count: recovery would be unreachable and every clean pass would
   re-halt. The pass counter belongs where the pass begins.

**Deviations of shape, none behavioural:**

3. **`WatchdogHealth` (both twins) carries two fields beyond §4.2's list**:
   `watching: int` and `last_failure_for_s: float | None`. §3.9's cells require
   both — "N position(s), last pass Ns ago" and "last <error_type> Ns ago" — and
   §4.2's field list cannot express either. The seam sweep forces the twins to
   match, so both carry them.
4. **`scan_age_s` reaches `watchdog.exit_started` through a new
   `ExitExecutor.bind_scan_age` seam** (§4.2's "the record at 3155" is a typo for
   `1155` — the record lives inside `ExitExecutor.close`). `PositionWatchdog
   .__init__` binds `self._scan_age` beside `bind_quotes`, exactly the existing
   precedent; the two fake executors in the tests gained the method. Unbound
   (a standalone executor), the payload carries `scan_age_s: None` — the "before
   any successful scan" case T13 asserts.
5. **`_note_exit_crash`'s detail carries `"where": "exit"` explicitly.** The
   surviving assertion from the replaced exit test reads the halt seam's
   `detail["where"]`, and §4.2's detail sketch omitted the key. It rides the
   record and the page too, where it is merely informative.
6. **§4.4's `status.WatchdogHealth` target spelling is incompatible with §4.5's
   own sweep.** `_target_keywords` requires the target construction to be a bare
   `ast.Name` call; `status.WatchdogHealth(...)` is an attribute and the sweep
   correctly failed on first run. The status twin is imported bare in
   `session.py` (beside its siblings) and the source type is annotated through a
   module alias, `argus.execution.watchdog as watchdog_types` — the two
   same-named types leave no cleaner option.

**Tests that could not survive verbatim, each keeping its load-bearing assertion:**

7. **T9 (`a_dead_audit_log_still_halts`) is adapted, not kept.** The design's
   table says "unchanged", but `await watchdog.run()` now loops forever on a
   generic failure — that is the change. The test drives one failing pass plus
   its settle with the dead recorder and asserts the same thing: the halt does
   not depend on the recorder (the `watchdog.degraded` write is suppressed, the
   halt fires anyway).
8. **T3 drives the REAL `run()` task**, not `check_once` directly. Mutation 3
   (a `return` re-added after the degrade) is invisible to a directly-driven
   pass; the loop itself has to be under test for the mutation to be caught, so
   the test runs `watchdog.run()` as a task on a 0.01s cadence, recovers the
   read mid-loop, and asserts the position is evaluated, triggered and exited.

**Mechanical consequences outside §4's file list:**

9. `tests/test_watchdog.py`'s `build_watchdog` gained a `stop` pass-through and
   its halt fake records `grade`; `RecordingExecutor` and `test_session_trading
   _side.py`'s `_Executor` gained `bind_scan_age`; `test_run_stops_cleanly_on
   _the_stop_event` reads `last_failure` (the §3.10 rename caught its one
   non-test reader); `tests/test_residual_orders.py` likewise.
10. `js["watchdog"]` gained `"health"` — `test_no_watchdog_still_publishes
    _neither_row` now pins the three-key shape (null health = shadow).
11. The credit-splat sweep (`test_open_spreads.py`) went from six credit-carrying
    payloads to seven: the crash page splats `credit_payload()` like every other
    page that names a held spread's credit.
