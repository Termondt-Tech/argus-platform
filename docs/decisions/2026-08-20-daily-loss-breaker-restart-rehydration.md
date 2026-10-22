# The daily max-loss breaker re-armed on every same-day restart (2026-08-20)

**Status: FIXED in code, not yet run against anything.** This one was found by
reading, not by an incident, and it is a LIVE-FLIP concern rather than a sandbox
one — cert cannot exercise it at all (below). Pre-live-min-size.

## What was wrong

`DailyLossBreaker` is a latch. Its own docstring says so: *"once tripped it stays
tripped until an explicit `reset()` at the next trading day's session start. A
recovering P&L must NOT silently re-arm trading — the day's risk budget is
spent."* That promise held for the life of one process and no longer.

`Application._run_session_day` builds the per-day risk state, and it built both
halves bare:

```python
breaker = DailyLossBreaker(self._audit, daily_max_loss=...)
state = SessionState(breaker=breaker, day_pnl=DailyPnl(), ...)
```

That is exactly right at a day rollover — neither may inherit yesterday's latch —
and exactly wrong at 11:40 when the 11:20 trip is still supposed to be in force.
Trip at 11:20, restart at 11:40 (a crash, a deploy, `systemctl restart`, an
`AuditWriteError` winding the supervisor down for a clean restart — the process is
*designed* to be restarted), and the new process comes up with the day's loss
budget handed back in full.

**Two independent failures, and the second is the worse one.**

1. **The latch is gone.** `initially_tripped` defaults to `False`, so the fresh
   breaker is un-tripped.
2. **The baseline moves.** `anchor_day_pnl_if_possible` anchors day P&L to
   *whatever NLV is current*, which after a loss is the post-loss NLV. Day P&L is
   `current_nlv - anchor`, so the day's damage now reads ~0 against the new
   anchor. The restarted breaker cannot re-trip on the loss already taken — it
   would need a *second* full `daily_max_loss` drawdown to fire. The rail is not
   merely reset; it is measuring the wrong thing for the rest of the day, and so
   is `/status`.

Neither failure covers for the other, which is why both had to be fixed. Fixing
only the anchor would leave a breaker that re-trips on the *next proposal* but not
before one — and not at all if NLV happens to be unreadable, which is precisely
when a rail should be loudest. Fixing only the latch would leave a tripped breaker
measuring against a wrong baseline for every later decision.

Nothing else latched across the restart either: the breaker does not engage the
kill switch, and the kill switch does not survive a restart (see the open decision
below).

## Why the seams existed but were never wired

`DailyLossBreaker.initially_tripped` and `DailyPnl(anchor_nlv=...)` were both
present, both documented in the present tense as the rehydration path, and both
reachable only from tests. `breaker.py` even named the mechanism — *"reconciliation
can find a `daily_loss.breach` already logged today and construct the breaker
already tripped"* — but no caller did. A parameter documented as a safety seam and
passed by nobody reads, at a glance, like a solved problem. That is the shape of
this defect worth remembering, more than the specific rail.

## Why cert cannot find this

Day P&L is DERIVED from NLV and cert never marks NLV, so `daily_pnl` is
identically 0.0 there and the breaker cannot trip at all — the structural inertness
already recorded in [the live-fire pass](2026-08-13-live-fire-sandbox-pass.md). A
sandbox session that restarts mid-day and behaves normally is not evidence about
any of this; it is a rail that was never armed either side of the restart.

The only proof available is a deliberate restart simulation, which is what
`tests/test_day_risk_rehydrate.py` is. Treat that file as the rail's coverage —
there is no environment that will catch a regression here.

## The fix

`risk/rehydrate.py` reconstructs both facts for a given trading date from the
audit log, and `_run_session_day` reads them before building the day's state.

- **The trip**: today's `daily_loss.breach` row →
  `DailyLossBreaker(initially_tripped=...)`.
- **The anchor**: today's FIRST `day_pnl.anchored` row →
  `DailyPnl(anchor_nlv=...)`.

`day_pnl.anchored` is new (Tier 1, written by `session.run_session` the once,
immediately after the anchor is set). Nothing durably recorded the day's reference
NLV before this — it existed only inside a `DailyPnl` instance — which is why the
seam could not have been wired without adding it.

**No new table, no migration.** The audit log already *is* the same-day state
store: single-writer, hash-chained, crash-durable, and indexed `(category, ts)` by
migration 0003 specifically so day-bounded lookups stay flat in total history. That
migration's own comment names "today's `daily_loss.breach`" as a lookup it serves.
A separate table would be a second place for the same fact to live and disagree.

Four properties are deliberate and each has a test that fails without it:

- **The rehydration seam is the `DailyPnl` CONSTRUCTOR, not the anchoring call.**
  `anchor_day_pnl_if_possible`'s existing already-anchored guard then does the
  right thing with no special case. Never weaken that guard to "re-anchor if the
  anchor looks stale" — anchoring to a post-loss NLV *is* the bug.
- **The day range is half-open on both ends.** The lower bound stops yesterday's
  trip being inherited; the upper bound stops a query keyed to one trading date
  sweeping up a later one's rows. A per-day latch leaks in both directions.
- **The FIRST anchor of the day wins.** Today the already-anchored guard means
  only one row is ever written per day, so `ORDER BY seq ASC` is unobservable in
  normal operation — which is exactly why it is pinned by a test. If a later row
  could win, each restart would re-anchor closer to the current NLV and the day's
  measured loss would shrink toward zero one restart at a time: the original bug,
  just slower.
- **A failed read HALTS.** "I cannot say whether the day's budget is spent" must
  never resolve to "it isn't". `DayRiskRehydrationError` → `_halt_process`
  (kill switch + stop, `source="day_risk_rehydrate"`), and the process parks
  rather than exits, as every other halt does. A row that exists but cannot be
  parsed counts as a failure, not as an absent row — degrading there would
  re-anchor to the post-loss NLV silently.

A restart into a day already under way writes `day_risk.restored` (Tier 1). It has
to: the breaker writes no second `daily_loss.breach` (the latch is idempotent by
design) and the anchor is not re-recorded, so without it an 11:40 restart is
indistinguishable in the trail from a process that ran since the open. The Slack
alert fires only for a restored **trip** — that changes what the desk should expect
for the rest of the day (the process is up, healthy, and will enter nothing). A
restored anchor alone is normal restart bookkeeping and gets the record and a log
line, not a page.

One incidental tightening came with it: the audit `ts` format now has a single
definition (`clock.UTC_TS_FMT`), and `daily_loss.breach` a single spelling
(`breaker.BREACH_CATEGORY`), imported by both the writer and the two day-bounded
readers. Three copies of a literal that must match is three chances to unwire a
safety rail with a rename and no test failure.

## The page had the same defect, one field over

`/status` did not read the breaker. It answered *"was there a `daily_loss.breach`
logged today?"* out of the audit DB and reported that boolean as
`breaker_tripped`; `StatusService` held no reference to the live
`DailyLossBreaker` at all.

Paired with the restart bug above, that composes into the worst available
outcome. The breach row survives the restart and the in-memory latch does not, so
the page rendered **TRIPPED in red while the gate was allowing entries**. An
operator glancing at `/status` under pressure saw the reassuring "halted" reading
at exactly the moment the system was silently still trading. Of the two possible
directions for that error, this is the one that costs money.

The rehydration above closes the restart case specifically — with the latch
restored, the row and the rail now agree. It was still fixed, because the
fragility is not the restart: it is **deriving live state from an indirect
source**, which is the same class of defect as C3 one layer up. Note that this
exact mistake had already been made and fixed in this file for the kill switch,
where a `kill_switch.trip` row "outlives the process that wrote it" and reporting
it as live state misrepresents what is happening (see the open question below —
that reasoning is what identified this one).

**The fix is the pattern already proven in the same module**, not a new one:
`halted_since` / `halt_reason` / `halt_source` are read through an injected
callable precisely because the halt happens after `StatusService` is built.
`breaker` is now a fourth such callable.

- `Application._breaker_for_today()` returns the live `DailyLossBreaker`;
  `_run_session_day` publishes each day's breaker the instant it is constructed,
  before the body runs, so a session that comes up already rehydrated-tripped is
  reported correctly from its first tick.
- **The callable hands back the OBJECT, not a bool.** Reading `.tripped` off the
  very instance `PreTradeGate` holds is what makes the page and the gate incapable
  of disagreeing; a bool-returning seam would let a future caller quietly supply a
  log-derived answer again.
- **The breach query is deleted, not merely unused.** One source of truth, and
  nothing left for a revert to reach for. `_DbStatus` carries no breaker field.

Two consequences worth stating plainly, because both are easy to "tidy" back:

- **It is trading-date-scoped.** `_day_breaker` holds the last breaker built and
  nothing rebuilds it between the close and the next session, so a bare reference
  would render Tuesday's TRIPPED all through Wednesday's pre-open — the same
  misreport, mirrored. The latch is per trading date; so is the report of it.
- **`breaker_tripped` is now `bool | None`, and None is a real answer.** It means
  *no breaker is armed for today*: no session has built one (before the process's
  first session, or a halted process that never got that far), or the one held
  belongs to a past day. Flattening it to `False` would render a green "not
  tripped" for a rail that does not exist — the same over-claim in miniature. JSON
  serializes `{"tripped": null}`; the HTML shows a muted "not armed (no session)".

## Verification

`tests/test_day_risk_rehydrate.py`, mutation-tested — each of these was reverted
individually and the named tests confirmed to fail, rather than assumed to cover:

| Reverted | Caught by |
|---|---|
| `initially_tripped=day_risk.breaker_tripped` → `False` | `test_a_same_day_restart_comes_up_STILL_TRIPPED`, `test_the_restored_trip_actually_denies_at_the_gate` |
| `DailyPnl(anchor_nlv=...)` → `DailyPnl()` | the same, plus `test_a_restart_BEFORE_any_trip...` and `test_repeated_restarts_all_rehydrate_to_the_FIRST_anchor` |
| the breach query's upper day bound | `test_a_session_for_a_PAST_date_does_not_see_todays_state` |
| `ORDER BY seq ASC` → `DESC` | `test_the_FIRST_anchor_of_the_day_wins_over_a_later_one` |
| `breaker_tripped` derived from a breach row again | `test_breaker_state_is_read_from_the_object_never_from_the_audit_log` and `test_breaker_tripped_is_none_when_no_breaker_is_armed` (`test_status.py`), plus both status tests below — 5 failures |
| `_breaker_for_today`'s trading-date check | `test_status_does_not_report_YESTERDAYS_breaker` |
| publishing `_day_breaker` deferred past the body | `test_status_reports_the_LIVE_breaker_and_agrees_with_the_gate`, `test_status_does_not_report_YESTERDAYS_breaker` |

The restart tests are real process lifetimes against one database file — two
`Application` instances, the writer lock genuinely released and re-taken between
them — not a re-constructed `SessionState`. The tripped/anchored state is asserted
**as handed to the session body**, before anything re-evaluates it; reading the
flags afterwards would pass on the strength of the re-evaluation and prove nothing
about the latch.

Coverage also pins the directions this must NOT break: a restart before any trip
stays tradeable, the day's first session restores nothing and stays quiet, and a
new trading day inherits neither the trip nor the anchor.

The `/status` half is covered from both ends. `test_status.py` pins the read at
the unit level in **both directions** — a breach row on disk with an untripped
rail reports `False`, a tripped rail whose row was never written reports `True` —
so it cannot be satisfied by a log read that happens to agree today, and it
asserts structurally that `_DbStatus` carries no breaker field at all. The
app-level pair in `test_day_risk_rehydrate.py` asserts the page and the rail
agree **as objects** (`snap.breaker_tripped is rail.tripped`), read from inside a
live session after a real restart, rather than that both happen to be `True`.

## Deploy

`src/` only — no config key, no migration, no `argus.toml` change. It takes effect
on the next session after the image is deployed. An existing `argus.db` has no
`day_pnl.anchored` rows, so the first session after the deploy simply records one;
nothing is backfilled and nothing needs to be.

One externally visible change comes with the `/status` fix: `daily_loss_breaker.
tripped` in the JSON can now be `null` (no breaker armed for today) where it was
always `true`/`false`. Nothing in the repo consumes that key, but anything the desk
scripts against it must treat `null` as "not armed", never as falsy-therefore-fine.

## Open: should a breaker trip also engage the kill switch?

Raised, not decided. Two things need to be on the record first, because the
premise the question was posed under does not survive reading the source.

**`kill_switch.since` does NOT prove the kill switch survives restarts.** It comes
from the newest `kill_switch.trip` row in the audit log
(`status._read_status_db`), which outlives the process that wrote it — `status.py`
says so in as many words. `KillSwitch.__init__` sets `_engaged = False`
unconditionally and holds no persistence of any kind. A steady date on `/status`
is a durable *row*, not durable *state*.

**So the adjacent finding is the bigger one: a same-day restart also re-arms the
kill switch.** A manual `/kill` at 10:00 followed by a restart at 10:05 comes back
un-halted, and unlike the breaker there is no second mechanism that would re-trip
it — nothing re-derives an operator's intent from P&L. This is the same class of
defect as the one fixed above, on the rail CLAUDE.md calls non-negotiable, and it
is out of scope here deliberately: it needs its own decision, because the kill
switch is PROCESS-lifetime with no reset, so "rehydrate it" immediately raises what
the expiry rule is (day-bounded? never? a manual re-arm?) — a question the breaker
does not have, since the trading day answers it.

Given that, coupling breaker→kill-switch today would **not** produce a second
independently-persisted layer. It would produce a second in-memory layer that
resets on exactly the same event, plus one behaviour change worth weighing on its
own merits: the kill switch also abandons an entry walk already in flight, which a
breaker trip currently does not. The recommendation is to treat the kill switch's
own restart behaviour as the primary question and decide coupling after it, rather
than couple to a rail with the same gap.
