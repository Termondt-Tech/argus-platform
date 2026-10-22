# The desk view hides a spent exit

**2026-09-14. BUILT the same day. `src/` only (`tools/desk.py`) + tests, no config
key, no migration, no new audit category — a purely REPORTING change, so the image
deploys alone.** Found in the EOD SOP review of v0.4.1's cert session, while
weighing the sandbox → live-min-size flip.

---

## What was wrong

On 2026-09-14 `tools.desk` reported the day like this:

```
OPEN / HELD  (marked at the watchdog's last look — NOT settlement)
  20260914-call-7625-7630              2     0.70     1.60    -0.90      -180.00  @11:34:10
                                                           unrealized      -180.00

CLOSED INTRADAY  (realized from actual fill prices)
  none — nothing was closed before expiry
```

One quiet held position, marked four and a half hours stale, and no mention of an
exit anywhere on the page.

What actually happened to that position:

| ET | |
|---|---|
| 10:29:04 | entry walk finishes `unresolved`; residual registered |
| 10:30:33 | `order.residual_filled` — **P5 adopts the abandoned order**, end to end |
| **11:33:10** | **`watchdog.triggered`** — the credit stop fires at 1.60 against a 1.40 stop |
| 11:33:13–11:34:45 | 7 × `walk.guard_unreachable_attempt` — `_ask_guard` absorbing it, process does not halt |
| **11:34:10** | the LAST `watchdog.check`. 2,410 of them, 2,402 priced |
| 11:34:56 | `order.submit_failed` → `watchdog.exit_finished` → **the attempt is spent** |
| 11:35:03–15:59:59 | **10,656 `watchdog.check_spent`** — still evaluated every ~1.5 s |
| 11:35:32–15:59:32 | 527 × `watchdog.exit_spent_still_held` |
| **15:30:00** | `watchdog.flatten_override` granted, `watchdog.exit_started` — to the second |
| 15:30:12–15:30:31 | `order.submit_failed`, `walk.disconnected`, `walk.finished` — the flatten dies too |

`walk.disconnected` × 7 and `order.submit_failed` × 4: cert's order API was
unreachable for this position's entire exit lifecycle. **Four exit attempts reached
`watchdog.exit_finished` and the desk view named none of them.**

## Two defects, one page

**1. `read_marks` read one mark category.**

```python
CHECK = "watchdog.check"
...
    "SELECT ts, payload FROM audit_log WHERE category = ? ...", (CHECK, start, end),
```

`watchdog.check` stops the instant a position's exit is spent and
`watchdog.check_spent` takes over — `_check_one` branches on
`self._attempted.get(key)` and `_attempted` is never pruned, so the move is
one-way. Reading the first alone froze the mark at **11:34:10, the last tick before
the exit died**, and discarded 10,656 later marks. The one row a desk most needs to
watch is the one row that stopped updating, at the exact instant it started to
matter.

**2. The exit outcome was read on the wrong branch.**

`read_exits` worked. `DeskDay.exits` was populated correctly, with all four
attempts' worth of information available. It was consulted in exactly one place:

```python
if ex.closed_contracts > 0 and ex.exit_debit is not None:
    ...
    f"{day.exits.get(candidate, 'closed')}"
```

— the CLOSED row. Where a position closed, the exit outcome is `filled` and adds
nothing you cannot read off the fill two columns to the left. Where a position did
**not** close, the outcome is the whole story, and the held row never asked for it.

## Why it is worth a record

**Nobody wrote a bug, again.** Both halves were correct when written. `read_marks`
dates from the tool's first commit, when `watchdog.check` was the only mark
category there was; `watchdog.check_spent` arrived on **2026-08-28**, from the pass
whose entire stated point was that a held, triggered spread had gone 76 minutes
with no surface able to say what it was worth. That pass gave the mark to the Slack
pages. `/status` did not get it either and was fixed on 2026-09-02
([a `/status` seam drops what its source gained](2026-09-02-a-status-seam-drops-what-its-source-gained.md)).
The desk view was the third surface and nobody looked.

So this is the same family as that record, but the mechanism is not a dataclass
copy and the rule from it does not reach here. **Two new ones do:**

- **A reader that names ONE audit category is making a closed-world claim about
  where a fact lives, and the runtime is free to add another.** A category filter
  cannot fail loudly: the rows are simply not selected, the query is valid, the
  result is well-formed, and the number it produces is a real number from a real
  record — just the wrong one. `MARK_CATEGORIES` now exists so the claim is stated
  in one place rather than implied at a call site.
- **A fact is most needed on the branch where it is least expected.** The exit
  outcome was read where an exit had obviously happened and not read where one
  might not have. That is backwards, and it is the reason the failure was
  *reassuring* rather than merely absent: the page did not say "unknown", it said
  nothing was closed, in the tone of an ordinary hold.

**And the class matters more than this instance, because of what the day was.**
Every rail fired correctly on 2026-09-14 — the credit stop on the right number past
its debounce, `GuardUnreachable` absorbed without halting the process (the exact
failure that killed 2026-09-03), the one-attempt rule holding, `check_spent`
continuing to evaluate, the `_SPENT` escalation bounding the silence, the flatten
firing to the second and claiming its one-shot override, P5 adopting an abandoned
order. It is the strongest exit-path evidence this project has. The instrument the
EOD SOP reads it through showed none of it.

**The P&L must not be read as evidence about anything.** Closing at 11:33 would
have realized −$180; SPX settled at 7620.995, the 7625 short call expired 4 points
OTM, and the position paid +$140. The day was **+$132.91 net of fees, not the
−$187.09 the page totalled** — that difference is the last-mark convention, which
the tool labels. The failure made money for the second time in two weeks (2026-09-03
was the first). One narrow-range session is where a stop is maximally wrong, the
same trap as 2026-09-02's $1,550.

## What was built

`tools/desk.py` only.

- **`MARK_CATEGORIES = (CHECK, CHECK_SPENT)`**, one query, `ORDER BY seq` across
  both, so the newest look wins whichever category it came from. Safe to merge
  because the transition is monotone.
- **`Mark.exit_outcome`**, read from `watchdog.check_spent` **before** the
  `cost_to_close is None` guard. Whether a position has an exit attempt left is not
  a question about quotes, so a trailing run of unpriceable ticks must not take the
  answer down with it — and neither must the asymmetric case where the last PRICED
  look predates the spend, which is the one a naive implementation gets wrong by
  answering `None`, i.e. "ordinary hold". Both directions are tested.
- **`read_exits` returns every attempt, not the last**, as a frozen `ExitAttempt`
  carrying `at_et`, `trigger_reasons`, the exit's own `reason`, and the broker's
  verbatim `reject_reason` off the walk — **two fields, never one**, on the
  [`mutation_error` precedent](2026-09-04-a-refusal-is-not-an-absence.md).
- **The spent state is on the held ROW** (`EXIT SPENT (nothing_at_broker)`), not
  only in a section below it: a position nothing ordinary will submit for again
  must not read as a normal hold on the one line an operator actually scans. A new
  `EXITS THAT DID NOT CLOSE THE POSITION` table lists every attempt, with a `WHY`
  list underneath on the `WHY THE BROKER REFUSED` precedent — broker prose is long
  and varies, and the operative question is which attempts share a cause.

**The note under the held row is precise about what SPENT does and does not mean,
deliberately.** An earlier draft said "nothing will submit for it again", which is
false for `NOTHING_AT_BROKER` and `CROSS_UNPRICEABLE`: both are in `_RETRYABLE_EXITS`
and retry under `_EXIT_SEND_ATTEMPTS`, and the flatten holds a further one-shot
allowance on top. The note now names the retry budget and the allowance, and says
manual only where neither applies. A monitoring surface that overstates a rail is
the same defect one direction over.

## Tests

Seven, in `tests/test_desk.py`, built on the 2026-09-14 shape and using real
`ExitOutcome` values:

- the mark comes from the latest look in **either** category;
- the spent state survives a final unpriceable tick;
- the spent state survives a last PRICED look that was an ordinary `check` — the
  asymmetric case, and the reassuring-when-wrong one;
- an ordinary held position carries no spent state;
- every attempt is kept, with times, trigger reasons and the broker's prose, and
  `reject_reason` is `None` rather than `""` when there was no walk;
- end to end through `_report`: the row says `EXIT SPENT`, the page says MANUAL,
  both attempts appear with their reasons;
- the CLOSED path is unchanged — last outcome on the row, no failure section.

Suite 2,121 (2,114 at `v0.4.1` + 7).

## What was deliberately NOT changed

- **Nothing in `execution/`.** The watchdog had every fact, wrote every record,
  paged 527 times and behaved correctly throughout. This was only ever a reader.
- **No new audit category and no payload change.** `watchdog.check_spent` already
  carried `exit_outcome` and the full evaluation; `watchdog.exit_finished` already
  carried the walk. Both were written on 2026-08-28 and 2026-09-02 respectively and
  needed nothing.
- **No arithmetic.** The tool's standing rule — *not a second scorer* — holds: the
  unrealized number now comes from a later mark, but it is still the watchdog's own
  `cost_to_close` through the same expression.
- **The settlement caveat.** A held position's terminal value still needs
  `tools.mark --from-feed`, and this does not extrapolate the last mark into
  something that looks like a result.

## What this leaves open

**`/status` has the same question and a different answer.** Its spent-exit row is
governed by `_SEAMS` and renders the mark (2026-09-02), so it does not share this
defect — but the two surfaces now describe a spent exit in different vocabularies,
and nothing asserts they agree.

**The operational half is not a code change and is owed.** `exit_spent_still_held`
paged ~18 times through B1's 900 s per-position throttle over four and a half hours,
and the position rode to expiry. In live, the same shape means: **a broker outage
during a triggered stop is eaten to settlement, up to the full width** — $860 on
this position. That is strictly larger than
[C5](../go-live-findings.md) (a stuck `Cancel Requested`) and should be written down
in its place, with the manual-close step named: when `exit_spent_still_held` pages
and the retry budget and the flatten allowance are gone, close it by hand.
