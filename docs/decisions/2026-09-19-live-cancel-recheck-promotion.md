# Promoting the cancel recheck in live without cert (2026-09-19)

*`watchdog.unmanaged_order_cancel` is `true` in cert and `false` in live, and the
recorded condition for promoting it in live — "until cert has a track record of the
real re-cancel" — can no longer be met. Cert has placed no entry since ~2026-09-02,
and on 2026-09-19 tastytrade support said the underlying sandbox fault has no ETA and
needs "a larger scope of dev work than initially anticipated". This record replaces
that condition with one live can meet by itself.*

> **SIGNED OFF 2026-09-19 (Richard), BUILT, and DEPLOYED in v0.4.5 (Arcturus) on
> 2026-09-20 ~09:00 CDT (~10:00 ET).** Drafted at his
> request after he rejected the cert condition as unrealistic and left open whether
> live needs the re-cancel at all. He signed off on the recommendation to keep it and
> promote it. `argus.live.toml` now reads `unmanaged_order_cancel = true`, and the
> guarding assertion in `tests/test_residual_orders.py` was edited to match.
> The §4 read was the pre-deploy gate (v0.4.5 deploy step 1); the operator verified
> the release himself.

## 1. What is being decided

P5 ([cancel recheck](2026-08-29-mid-session-cancel-recheck.md)) registers the one
order a finished walk may leave behind. While the broker **positively confirms** that
order is still WORKING, and it is an **opening** order, P5 asks for the cancel again
on a backoff schedule. `watchdog.unmanaged_order_cancel` decides whether that repeat
cancel is actually SENT (`true`) or only decided and recorded at Tier 2 as
`order.residual_would_cancel` (`false`, the `would_cancel` stage).

Two questions, in order:

1. **Does live need the re-cancel at all?** Richard said he might accept leaving it
   off. §3 argues it should be on.
2. **If it does, what has to be true before the flag goes to `true`?** §4.

## 2. Where the cert condition came from, and why it can't be met

The condition was **not** in P5's own design. That record's recommendation 2 sized
the `would_cancel` stage at *"a week, not a month, and do not wait for a
`would_cancel` record to appear before promoting"*. Its reasoning was that the stage
checks the INPUTS (direction, OPEN/CLOSE split, identifier cross-check, how the
sighting is read), while the trigger itself is rare by construction.

The cert-first gate came in afterwards, through
[config profiles](2026-09-04-config-profiles.md) **F2**. The live file had bundled the
promotion into the cutover, and F2 reset live to `false` so the first real cancel
would not land on the day of the first real order. That sequencing concern was
reasonable. The allowlist note in `tests/test_config_profiles.py` then went further,
to *"until cert has a track record of the real re-cancel"*. That makes the gate an
observed EVENT, not a stage of fixed length.

That event can no longer happen:

- **A residual needs an entry walk, and cert has had none since ~2026-09-02.** Cert
  lost the 5-point SPXW strike ladder (`instrument_validation_failed` on every
  strike ≡ 5), so no 5-wide entry can be built there. That leaves no opening orders,
  so no opening residuals, and nothing for the re-cancel to act on. The flag has been
  `true` in cert since v0.3.21 (2026-09-08), and the path has been unreachable there
  for almost all of that time.
- **Argus no longer runs in cert.** The droplet has loaded `argus.live.toml` since the
  2026-09-15 cutover, and compose mounts only the active profile.
- **Even working, cert could not have shown the hard case.** The case that matters is
  an order stuck at `Cancel Requested`, or a cancel DELETE that errors. Cert fills
  are undocumented and unrealistic ([`CLAUDE.md`](../../CLAUDE.md): *a number
  measured in cert cannot size a change on its own*). A cert re-cancel that landed
  would have proved the DELETE call, and §4 shows live already proves that.

Leaving the condition in place turns "not yet" into "never" without anyone deciding
that. This record makes the decision explicitly.

## 3. Does live need it? Yes.

**The kill switch rule depends on it.** [`CLAUDE.md`](../../CLAUDE.md) requires a
halt to stop new entries *"including an entry order already working at the
exchange"*. It reaches that order through `HaltCheckFn` → `_abort` →
`_request_cancel`, and `_request_cancel` swallows every exception. P5 exists because
that promise was best-effort: on 2026-08-28 a cancel DELETE returned a 502, and the
order was still working two hours later. With the flag `false` in live, P5 records
that the order should be cancelled and sends nothing. **A `/kill` whose one cancel
fails leaves the order resting, and nothing asks again.** Leaving the flag off
accepts that the kill switch rule is best-effort on the real account.

**What leaving it off costs is bounded but real.** A residual opening order that stays
working:

- counts against `max_concurrent_positions = 1` through P4's
  `working_open_spread_count`, so the welded `1 × 950 ≤ 950` invariant holds and no
  second position can open beside it;
- if it fills, books through `FillConsumer`, and the watchdog adopts it with a virtual
  stop, a mark and the 15:30 flatten. That happened end to end in cert on 2026-09-01.

So the harm is not uncapped exposure. It is **a position the operator told the system
not to open**, which the kill switch rule exists to rule out. The benefit of the rule
does not shrink at minimum size. It is also the one case this project cannot
reconstruct later from the audit log.

**What turning it on risks is very small, and it only ever moves toward less
exposure:**

- **The recheck makes no new decision.** `_request_cancel` has already decided this
  order must not stand. P5 only repeats a request that was already made, for an order
  the broker says is still working.
- **An opening order is the only thing it ever cancels.** Direction comes from what
  Argus MINTED, cross-checked against `parse_external_identifier`. A disagreement
  refuses (`direction_disagreement`), and a CLOSING order is paged and never touched
  under any grade or halt.
- **Only a positive WORKING sighting is acted on.** Absent, unreadable and ambiguous
  are read gaps: they neither resolve the residual nor authorize a cancel.
- **The worst possible wrong action is a spread not opened.** If the order fills
  between sighting and cancel, the DELETE fails, the fill books, and P5 resolves
  `FILLED`. If it partially fills, the remainder is cancelled and the position is
  smaller than the gate approved, never larger.
- **It is rate-bounded.** Backoff of 15/30/60/120/240/480s, then flat at
  `watchdog.unmanaged_order_max_s = 900`: about 30 requests for a four-hour stuck
  order, under `ratelimit.py`'s process-wide governor.

The costs are lopsided. Off, the failure is an unwanted live position after a
`/kill`. On, the failure is at worst a missed entry. The feature should be on.

## 4. The new precondition: live's own `would_cancel` record

**The outbound call is not new in the CODE, and the gate read showed it is not yet
proven in the MARKET — see §4.1, which corrects this paragraph's first draft.** P5's
`cancel` is wired to `stack.execution.cancel` (`session.py`), the same
`AuditedOrderExecution.cancel` port that `_request_cancel` calls on every walk that
walks away or aborts (`loop.py`). It writes the same `order.cancel_attempt` /
`order.cancel_requested` / `order.cancel_failed` records at the same tier. P5 adds no
cancel record types of its own, and is one more caller of that call rather than a new
path to the broker. **What the first draft of this record went on to claim — that
production's acceptance of an Argus cancel DELETE "is answered by every live walk that
has ever walked away" — is false**: no live walk has ever walked away, so live has sent
no cancel at all. §4.1 has the measurement and what survives of the argument.

**What is new is the decision, and `would_cancel` has been exercising it on live data
since 2026-09-15.** That is the stage P5 designed. It is a better witness than cert
ever was, because it runs against production's order states, production's
relabelling and production's timing.

So the precondition becomes:

> **P-live.** Read `argus-live.db` **read-only** from the cutover (2026-09-15 16:30 ET)
> to the read date. For every `order.residual_registered`,
> `order.residual_would_cancel` and `order.residual_unmanaged` row:
>
> 1. the order's `external_identifier` parses to one of Argus's OWN identifiers,
>    and every `order.residual_would_cancel` row is an **opening** one;
> 2. no `direction_disagreement` tag appears (it should be unreachable, and seeing
>    it means an invariant broke);
> 3. every `order.residual_would_cancel` row names an order that the broker's own
>    order history shows was **still working** at the row's timestamp, meaning a
>    cancel then would have been correct;
> 4. nothing in the set is a CLOSING order carrying a would-cancel decision.
>
> **If every row passes, promote.** **If any row fails, do not promote** — that is a
> defect in the decision, and it gets its own record.

**Zero rows is the likely result, and it also means promote.** P5 predicted it: most
residuals resolve on the first poll (in cert, both 2026-08-31 residuals went
cancel-requested → DEAD within 1.5s), so `would_cancel` rows are rare by construction.
Waiting for n ≥ 1 on a path that only fires after something has already gone wrong
could take weeks, and for all of that time the kill switch rule stays best-effort on
the real account. With zero rows, §3's structural argument and the unit and
structural tests in `tests/test_residual_orders.py` carry the decision. P5 already
reached that conclusion in its own design (*"do not wait for a `would_cancel` record
to appear before promoting"*).

The read is **read-only** (`?mode=ro`), per the never-touch-the-live-database rule.
Richard pulls the database; nothing in this repo reaches the droplet. The query:

```bash
sqlite3 "file:argus-live.db?mode=ro" "SELECT seq, ts, category, payload FROM audit_log WHERE category LIKE 'order.residual%' ORDER BY seq"
```

### 4.1 What the gate read found (2026-09-20)

Run against the nightly snapshot `argus-20260919T163001.db` (taken 2026-09-19 16:30 ET,
a Saturday, so it holds every live record through the 2026-09-18 session), read-only, on
the workstation. **The file is the live database**: `seq` 1 is
`process.start {"environment": "live", "version": "v0.4.2"}` at 2026-09-15 16:33 ET, the
cutover boot, and it runs to `seq` 59,877 at 2026-09-18 16:26 ET.

**The gate passes on the zero-row branch.**

| Query | Result |
|---|---|
| `category LIKE 'order.residual%'` | **0 rows** |
| `order.residual_registered` | **0** — no walk has ever left an order behind in live |
| `category LIKE '%cancel%'` | **0** — no `order.cancel_attempt`, `_requested` or `_failed`, ever |
| `walk.finished` | **4**, all `outcome: filled`, all `steps: 0`, all `cancel_requested: false` |
| `positions` | **0 rows** (flat) |

So all four checks are vacuous, there is nothing that could fail one, and none of the
four live walks — two entries (2026-09-17 `put-7585-7580` at 0.50, 2026-09-18
`put-7595-7590` at 0.55) and their two 15:31 ET flatten closes (both at −0.05) — ever
repriced, let alone walked away. P5 has had no occasion to register anything, which is
exactly why the cert gate was the wrong instrument: it was waiting on an event whose
live counterpart has not happened either, in a world where **all four walks filled on
their opening limit**.

**What this costs the argument, precisely.** The re-cancel's outbound DELETE has never
been sent in production by any caller. The claim that it was already proven live was
wrong, and it is withdrawn. Three things survive, and the conclusion does not change:

1. **The flag does not decide whether live ever sends a cancel.** `_request_cancel` runs
   on any walk that walks away, aborts, or times out, whatever this flag says. The first
   live cancel DELETE will come from the walk itself, not from P5.
2. **For the two dominant abandonment classes, P5's send is a REPEAT.** Registration
   happens at `_finish` on an order whose last observation was not terminal. Where the
   walk asked and the DELETE raised, or asked and the order stuck at `Cancel Requested`
   (`cancel_requested: true`), the port has already been exercised on that very order and
   P5 re-asks. Only the third class — the walk could not read the order and never asked
   (`cancel_requested: false`) — can make P5's call the first for that order, and there
   it is the same call `_request_cancel` would have made had it been able to.
3. **An unproven DELETE cannot be worse than not sending one.** If production refuses it,
   the result is `order.residual_cancel_failed` at Tier 2 and the backoff continues —
   which is observer mode's outcome plus a record. The risk of the call is not that it
   fails; it is that it succeeds against something it should not have, and that is what
   §3's four refusals bound.

**So the first live re-cancel is still a first, and it is on the watch list** (§5, and
v0.4.5's "after deploy" step 2). What the gate settles is that nothing in live's own
record argues against the promotion; what it cannot settle is broker behaviour nobody
has observed yet in production.

### 4.2 What was added after the deploy (2026-09-20)

Not part of v0.4.5's deployed scope — both land in the tree for the next release, and
neither changes runtime behaviour. They exist because the promotion made a seam
load-bearing that had been covered in halves.

1. **`tests/test_kill_reaches_a_working_order.py`** asserts the non-negotiable as ONE
   chain: `POST /kill` over a real socket → the walk aborts → the first cancel DELETE
   raises → the order is still `Live` at the broker → registration → the watchdog's
   own `get_live_orders()` answer → the second cancel → **the order is dead at the
   broker**, asserted on the broker's state rather than on a spy. The recheck's
   `cancel` is the same `AuditedOrderExecution` the walk uses, wired as `session.py`
   wires it, because "one more caller of the call the walk already makes" is the
   argument this record rests on and a separate spy would assert around it. Two
   companions: the identical chain in `would_cancel` mode sends nothing and records
   the decision (the rollback's behaviour, which has to keep working), and a
   ten-minute blind poll window neither sends blind nor retires the residual — the
   RATE-not-COUNT bound, on the correlation that makes this backstop's worst case its
   own trigger. Both halves were verified to fail under mutation (the outbound cancel
   disabled; registration skipped at `_finish`).
2. **A kill-side SOP in `README.md`**, beside the kill-switch endpoint. It covers only
   what P5 cannot reach — a restart after a kill (the registry never rehydrates), an
   unreachable `/kill`, and an order whose id was never learned — plus the measured
   4–5s between the press and the cancel, and the rule the human inherits verbatim:
   **cancel `argus-open-…`, never `argus-close-…`.**

## 5. What is NOT changed

- **The code.** No change to `residual_orders.py`, the registry, the licence, the
  backoff, or the single guarded outbound call. The flag is still the only switch, and
  an AST test still pins it to exactly one call site.
- **Closing orders are never cancelled**, under any grade or halt.
- **Residuals are never rehydrated across a restart.** A previous process's orphan
  is still reconciliation's to find, and it still halts.
- **The stuck-at-`Cancel Requested` case stays unexercised.** Promoting does not
  prove it; it makes the system keep asking while it lasts. After the flip, the first
  `order.residual_cancel_failed`, or a `residual_order_working` page past its second
  attempt, is an event to read carefully, not a routine rail firing, and gets written
  up in the week's EOD review. **As of the gate read, live has never sent a cancel
  DELETE of any kind** (§4.1), so the first one — from the walk or from P5 — is the
  first in production.
- **Cert and dev stay as they are.** Cert stays `true` (the allowlist note says to
  retire the divergence "by flipping live, never by flipping cert back"). Dev stays
  `false`, because dev registers no residuals and the flag does nothing there.
- **The exit re-attempt open decision** is untouched, as P5 required.

## 6. Rejected alternatives

- **Keep waiting on cert.** Rejected: §2. With no ETA and entries dead there, this is
  "never", decided by omission.
- **Leave it off in live permanently.** Rejected: §3. It makes a kill switch rule
  best-effort on the one account where it matters, to avoid a risk that is at worst
  a missed entry.
- **Prove the path with a deliberate live test** (for example, a far-OTM opening order
  left resting, then a forced DELETE failure). Rejected. It needs a way to make a
  cancel fail on purpose, which production does not offer. Faking the failure in
  process tests nothing the unit tests don't already test. And it places a real order
  for no trading reason, against
  the broker-side-effect discipline.
- **Promote in dev first.** Rejected: dev registers no residuals
  (`dry_run_submissions` routes nothing), so it would prove nothing.

## 7. The release, once signed off and §4 is read

A **config-only release on its own day**, outside the session window. The
`config:` class restarts the process and nothing else.

1. `argus.live.toml`: `unmanaged_order_cancel = true`. Rewrite its comment block to
   point here instead of at the cert stage, and note that live now actually cancels.
2. `tests/test_config_profiles.py`: retire the `("sandbox", "live",
   "watchdog.unmanaged_order_cancel")` allowlist entry, which
   `test_the_allowlist_has_no_stale_entries` will require. Add a
   `("dev", "live", …)` entry carrying the existing dev/sandbox reason, which
   `test_value_divergence_is_declared` will require, since dev stays `false`.
3. Docs: `docs/live-cutover-runbook.md` §8 (both bullets), `docs/go-live-findings.md`
   (the `unmanaged_order_cancel` row), `docs/architecture/00-index.md` item 10, the
   `CLAUDE.md` current-phase paragraph ("its live promotion is a post-cutover
   config-only release of its own"), and a status line on this record.
4. Deploy with `argus-deploy.sh`. It reads the config fingerprint back after the
   restart, and the new sha12 is what shows the file loaded.
5. **Confirm by the config fingerprint**: the new `config.sha` on `/status` and on
   the startup line. The `unmanaged` section can't confirm the mode on an ordinary
   day. It names the mode (`auto-cancel ON for opening orders` versus `OBSERVER
   MODE`) only while a residual is outstanding, and reads `none working` otherwise.
   The first residual after the deploy is when the page shows it.

**Rollback** is the same one-line edit back to `false` and another config-only
release. Unlike the `host.env` rollback, it does not strand a held position: the
flag touches opening orders only and leaves the watchdog, the exits and the flatten
alone. It is still a restart, though, so do it outside the session window like any
other.
