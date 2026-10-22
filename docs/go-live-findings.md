# Go-live readiness

**What this file is:** the tracking list for the sandbox → live-min-size flip.
Three sections, ordered by *"can this stop the flip?"* — the gates, the mechanical
pre-flip checklist, and everything else.

> # ✅ THE FLIP HAPPENED. ARGUS WENT LIVE 2026-09-15 AT 16:30 ET.
>
> **This file's purpose — tracking what gates the sandbox → live-min-size flip —
> is discharged.** Both gates are closed: G1 accepted in writing 2026-09-14, **G2
> closed by performance 2026-09-15** (§5.1 makes the post-close flip the
> rehearsal). The pre-flip checklist is complete. Running now: **v0.4.2**, profile
> `live`, config sha `98fcd21981a3`, production account `5XX00000`, database
> `argus-live.db`.
>
> Sections 1 and 2 are now a **record** of what was decided and confirmed, kept
> because the reasoning in G1 and C5 is live operational guidance and not history.
> **Section 3 is the part that is still forward-looking**, and it is now simply the
> backlog — nothing in it was ever a gate, and there is no longer a flip for it to
> not gate.
>
> The record of the cutover itself is
> [`docs/live-cutover-runbook.md`](live-cutover-runbook.md)'s header.

**Last full re-verification: 2026-09-10**, against source, the three config
profiles, `git tag`, the test suite (2076 passed), and the live `/status`
snapshots in `docs/status-snapshots.txt`. Running in cert at that time:
**v0.3.22**, profile `sandbox`, config sha `cb69db256e27`; `v0.4.1` was tagged and
deployed 2026-09-13, and **`v0.4.2` is what the cutover was performed on**.

**Provenance.** The original audit was authored against the `v0.3.13` tree
(2026-09-03, `d03fef7`) and carried 29 findings. As of 2026-09-14: **21 closed,
5 partly closed, 3 open — NONE of them blocking**, G1 and C5 both having been
accepted in writing that day. The closed set is at the bottom as one line each;
the narrative for any of them lives in `docs/decisions/` and `docs/releases/`.

**The one thing that was still owed was not a finding: G2's rehearsal, which is not
separable from the act.** The post-close flip *is* the rehearsal (runbook §5.1) —
**performed 2026-09-15, so G2 is closed.**

> ### Where this stood, 2026-09-14 — the day before the flip
>
> **C8 is closed.** The last non-Argus position in the live tastytrade account
> closed today, under normal trade management rather than forced for the flip.
> That was the one item waiting on the market rather than on a decision, and it
> was the gate under **G1** (a whole-account NLV anchor is only the right quantity
> in a dedicated account) and under the runbook's §1.1 (a fresh `positions` table
> reconciles clean only against a flat book).
>
> **Both droplet reads came back clean on 2026-09-14** (see C4 and G2), and **both
> remaining decisions were written the same day:**
>
> | | Item | Shape |
> |---|---|---|
> | ~~G1~~ | ~~daily max-loss breaker never exercised~~ | **ACCEPTED IN WRITING 2026-09-14 — see G1, including the distinction day 1 does and does not exercise** |
> | ~~C5~~ | ~~stuck `Cancel Requested`~~ | **ACCEPTED 2026-09-14, and BROADENED: the class is "the broker will not take a closing order", whose other member was observed for 4h25m the same day** |
> | ~~C4~~ | ~~NAS pull green streak~~ | **done 2026-09-14 — 8 consecutive `ok` days** |
> | ~~P1~~ | ~~snapshot source line~~ | **done 2026-09-14 — confirmed on two consecutive nightly runs** |
>
> Plus the one pre-flight that is not a finding: confirm `/status` reports
> `v0.4.1` and that it has run a clean cert session.
>
> **Nothing left is a build, nothing left is a read, and nothing left is a
> decision.** What remained is **G2's rehearsal, which is not separable from the
> act** — the post-close flip IS the rehearsal (runbook §5.1).
> **THAT CUTOVER WAS PERFORMED THE NEXT DAY, 2026-09-15 at 16:30 ET.**
> → [`docs/live-cutover-runbook.md`](live-cutover-runbook.md)
>
> **One thing the 2026-09-14 cert session changed about the case for flipping, and
> it is not a finding on either list.** The entry path in cert is dead — 5 of 7
> attempts refused `instrument_validation_failed`, a broker-side instrument-catalog
> defect stopping Argus since 2026-09-02 — so another cert day yields no entry-path
> information. But the same day drove the EXIT path end to end under a broker
> outage and **every rail fired correctly**: the credit stop on the right number
> past its debounce, `GuardUnreachable` absorbed without halting the process, the
> one-attempt rule holding, `check_spent` still evaluating, the `_SPENT` escalation
> bounding the silence, the 15:30 flatten firing to the second and claiming its
> one-shot override, and P5 adopting an abandoned order end to end. That is the
> strongest exit-path evidence this project has, and it argues *for* the flip.
> → [the desk view hides a spent exit](decisions/2026-09-14-the-desk-view-hides-a-spent-exit.md)

**Section 3 is a backlog, not a checklist.** It will eventually become its own
document. Nothing in it gates the flip.

---

## 1. Go-live gates

Two. Each had to be *decided* before the flip, not necessarily *built*.
**Both are now closed — G1 accepted in writing 2026-09-14, G2 by performing the
cutover 2026-09-15.** G1's content below is not history: it is the operating
guidance for a rail that is now in force and still unproven.

### G1 — The daily max-loss breaker has never been exercised

**Was blocking. ACCEPTED IN WRITING 2026-09-14 — see the acceptance below.** Day
P&L is structurally 0 in cert — NLV is never marked
(`/status` 2026-09-10: `nlv: 100000.0`, `daily_pnl: 0.0`) — so the trip, the NLV
anchor and same-day rehydration are test-only. The plumbing is built
(`risk/rehydrate.py`, the `day_pnl.anchored` Tier 1 record); the mutation-tested
restart simulation is the rail's only coverage, and cert cannot add to it.

**This rail is the reason C8 is not optional** — `DailyPnl` is whole-account NLV
minus a whole-account anchor, so a single non-Argus position in the account would
make the rail's first-ever exercise also its first measurement of the wrong
quantity.

### G1 — ACCEPTED IN WRITING, 2026-09-14 (Richard)

**Accepted: the first live session is the first real exercise of the daily
max-loss breaker, and it goes live unproven.** No further build gates the flip on
this rail. Cert cannot add to its coverage and never will; the mutation-tested
restart simulation remains its only coverage until live produces a marked NLV.

**What day 1 does and does not exercise — this is the half that was never written
down, and it changes what to watch for.** The rail has four parts and they go live
on different days:

| Part | First exercised |
|---|---|
| **The anchor** — first live NLV read becomes the day's baseline (`day_pnl.anchored`, Tier 1) | **Day 1, within seconds of the first account read** |
| **The measurement** — `daily_pnl` moving as NLV moves, and `/status` rendering it off the live object | **Day 1, continuously** |
| **Same-day rehydration** — a restart re-reading today's anchor rather than re-anchoring to a post-loss NLV | Day 1 *only if* the process restarts |
| **The trip** | **Not day 1, and probably not for weeks** |

**The trip is nearly unreachable at live-min size, and that is the welded invariant
working, not a gap.** `max_concurrent_positions = 1` and `daily_max_loss = 950.00 =
1 × max_loss_per_position`, exactly. With one position a day, the day's loss only
reaches 950 if that single spread goes to very near its full width. So the realistic
day-1 outcome is *"the breaker measured correctly and did not trip"* — which is a
real result for the anchor and the measurement, and **says nothing about the trip**.
Do not read a quiet first week as the rail being proven. The acceptance therefore
has two halves, and the second one persists well past day 1.

**The single most valuable day-1 observation is that `daily_pnl` is not identically
zero.** In cert it is 0.0 by construction because NLV is never marked. If live
`/status` shows `daily_pnl: 0.0` while a position is open and marked away from its
entry, the rail is inert in live too — and that is this gate firing, not a quiet
day. Check it early, with a position on.

**From v0.4.4 the page says when a zero is a placeholder** (§10 of the v0.4.4
notes): `account.daily_pnl_anchored: false` with `daily_pnl: null` means the day has
no P&L anchor, so the breaker is reading `DailyPnl`'s placeholder 0.0 and cannot
trip. That is the gate firing too, from a different cause. A `0.0` beside
`daily_pnl_anchored: true` is a real reading.

> **DEFERRED BY THE CALENDAR, 2026-09-15.** Day 1 after the flip is **2026-09-16, a
> known FOMC blackout day**, so Argus places no entries and holds nothing. On a flat
> account with no entries and no cash movement NLV does not move, so `daily_pnl` is
> *legitimately* exactly `0.0` — **indistinguishable from cert's structural zero,
> which is the precise failure this observation exists to detect.** A zero that day
> is not evidence in either direction. **The first real G1 read is the first day
> Argus actually holds a position.** The clause above — *"with a position on"* — is
> load-bearing and was written before the calendar was checked.
>
> The one G1 input that *is* readable on a blackout day: `account.nlv` rendering the
> real balance instead of cert's frozen `100000.0`, once the session builds
> its first `RiskSnapshot` at ~09:45 ET. That confirms the input is readable; the
> delta half waits.

**The manual cross-check, day 1 (runbook §6).** Three numbers, two of which should
agree and one of which deliberately will not:

1. **`/status`** — `daily_pnl`, and the breaker row (read off the live object,
   trading-date-scoped, never a `daily_loss.breach` audit row).
2. **By hand from the broker** — current account NLV minus the NLV at the anchor
   instant, taken from the tastytrade app or web. This is the same arithmetic
   `DailyPnl` does, down a different pipe. **These two must agree closely.** A
   disagreement of dollars is measurement timing; a disagreement of hundreds is a
   finding, and the flip rolls back (one line of `host.env`).
3. **`tools.desk`** — realized + unrealized + fees. **This one will NOT match
   exactly and must not be expected to**, because it is a different quantity: desk
   is per-position, reconstructed from fills and the watchdog's own marks, while
   `DailyPnl` is whole-account NLV delta at the broker's marks and books fees on the
   broker's schedule. Close is confirmation; identical would be a coincidence.
   Treat a large divergence as a question about *which* is wrong, not as proof
   about either.

**Two preconditions that make the arithmetic true, and both are standing properties
rather than one-time checks.** The account must hold nothing Argus did not place
(C8 — re-confirmed each morning, kept true only by nothing else trading there), and
**no cash may move into or out of the account during a trading day**: `DailyPnl`
reads a transfer as P&L, and a same-day restart re-uses the day's original anchor,
so a deposit mid-session corrupts the rail in the direction of not tripping.

**What this acceptance does not cover.** The breaker only stops NEW entries and
cannot close what is already open — that division is spec §8's and is unchanged.
On an ordinary losing trade it is `credit_multiple` that bounds the loss, not this
rail. And a broker outage during a triggered stop defeats both (see C5).

### G2 — No written live-cutover procedure

**CLOSED 2026-09-15 BY PERFORMING IT.** The procedure was written 2026-09-11 and
executed 2026-09-15 at 16:30 ET on `v0.4.2` — profile `live`, config sha
`98fcd21981a3`, account `5XX00000`, database `argus-live.db`. The rehearsal was
never separable from the act (§5.1), so the flip discharged both halves at once:
the live OAuth registration authenticated, the live account reconciled clean, the
credential slot swapped with no `.env` edit, and the durable kill switch was
tripped and cleared on the live process. The runbook's header carries the full
record and remains the **rollback** procedure.

*What follows is the state of this gate going in.*

**Was blocking. The mechanism was done; the procedure was not.**

Everything the cutover depends on now exists (v0.3.17–v0.3.22): `argus.live.toml`
tracked, regenerated and pinned key-for-key by `tests/test_config_profiles.py`;
`argus-live.db` so live can never continue the cert hash chain; the profile in
`/etc/argus/host.env` (per host, not in git) via `EnvironmentFile=` and
`${ARGUS_ENV:?}` — **the cutover is one line of one file changing**; a deploy
script that resolves the profile, refuses a tag missing that profile's config, and
reads the config fingerprint back after every restart; and trading credentials
keyed on `(role, world)` so the cutover no longer hides a hand-swap of `.env`
values.

**WRITTEN 2026-09-11 — [`docs/live-cutover-runbook.md`](live-cutover-runbook.md).**
The procedure now exists, written against the `host.env` mechanism and verified
against source, the three profiles and `scripts/argus-deploy.sh`. It is NOT yet
rehearsed. It surfaced **one blocker of its own, FIXED 2026-09-11 and DEPLOYED
2026-09-13 with `v0.4.1` (`scripts/` + `tests/` only, inert for the restart
classifier, so it rode the tag's checkout with no separate step):
`scripts/argus-snapshot.sh` opened `/data/argus.db` by literal path**, so after
the flip the nightly snapshot would have backed up the frozen CERT database while
every observable downstream stayed green — the copy succeeds, `verify_chain`
passes on an intact-but-finished chain, a fresh `argus-<stamp>.db` lands nightly,
and `argus-pull-check.sh` reports `ok`. The live audit log would have had no
off-volume copy and nothing would have said so. **Created by v0.3.17's own
`db_path` fix (F1) — closing one cutover blocker opened another, one machine
over.** Same class as v0.3.21's unprivileged-run misreport and `docker compose
run` vs `docker exec`: **a check performed against the wrong subject proves
nothing about the right one.** Fixed by resolving the source through
`Config.load()` with no argument — the same call `python -m argus` makes, so the
backup finds its source the way the process finds its destination — with the
source printed every run, a missing source refusing loudly, and a test asserting
the ABSENCE of any `.db` literal rather than the presence of the loader. → runbook §2.

`docs/argus-deployment-runbook.md` §16 is the *chunks 1–4* cutover (timer model →
always-on supervisor), not this one, and `docs/argus-cutover-runbook.md` is a
superseded draft of §16. Three cutover documents, one live.

**Updated 2026-09-14 — the journal read is DONE and clean.** Two consecutive
nightly runs (Sep 13 and Sep 14, both 16:30 ET) printed
`argus-snapshot: source /data/argus.db (profile sandbox, environment sandbox)`,
followed by `verified ... chain intact` (757,067 then 780,853 records). The fix is
deployed *and* demonstrably resolving through the loader, which were two separate
facts; both are now established, and the cert-era line is on record as the control
to compare the first live one against.

**What was still needed to close G2: rehearse — DONE 2026-09-15.** The rehearsal
is not separable from the act; the runbook's §5.1 timing exists because a
post-close flip IS the rehearsal (the supervisor idles outside its session window
but still reconciles at process start, so the first live boot proves the OAuth
registration, the account pin and a clean reconcile with no opportunity to trade).
**It proved exactly that, in that order, and nothing traded between the proof and
the decision to keep it.** This was step 4 of the config-profiles migration; the
migration is now complete.

**Two steps the procedure has to carry** (folded in here 2026-09-10 when the
credentials row closed — they are runbook steps, not a gate):

- **`credcheck` before the flip, via `docker exec` on the running container.**
  Already done once, 2026-09-10, clean on all three slots. Repeat it as a
  pre-flight, because the values can be edited between now and cutover day and the
  cost of catching a mechanical mistake there is nil.
- **Treat the first live boot as a step that can fail on credentials.** `credcheck`
  reports shape, never authentication — its own output says so: *"the values in
  that slot are well-formed but come from DIFFERENT app registrations"* is exactly
  the failure it cannot see, and the three trading tools each `raise SystemExit` on
  `environment == "live"`, so the first live `/oauth/token` call happens inside the
  cutover. Nothing needs building for this; the rollback is the same one-line
  `host.env` revert, and knowing that in advance is the whole mitigation.

---

## 2. Pre-flip checklist

Mechanical, tickable, no narrative. None of these needs a design.

| # | Item | Done when | State |
|---|---|---|---|
| C1 | **Live-min-size risk caps decided** | The four values are set deliberately and `tests/test_config_profiles.py` passes | **Done 2026-09-11 — full suite green (2113 passed).** Resolved by syncing cert to live's min-size numbers rather than by re-declaring the divergence: `argus.sandbox.toml` now carries `max_concurrent_positions = 1` (was `2`), `max_loss_per_position = 950.0` (was `1250.0`), `daily_max_loss = 950.00` (was `2500.0`, welded `1 × 950`); `argus.dev.toml`'s `max_loss_per_position` synced `1250.0 → 950.0` to keep matching its own "carries the sandbox value" design (it already carried `max_concurrent_positions = 1`). `max_contracts_per_position = 2` was already common to all three and untouched. `tests/test_config_profiles.py`'s `ALLOWED_DIVERGENCE` cleaned up rather than left stale: retired `(sandbox, live, risk.max_loss_per_position)`, `(sandbox, live, risk.daily_max_loss)`, `(dev, live, risk.max_loss_per_position)`, `(dev, sandbox, risk.max_concurrent_positions)`, `(dev, live, risk.max_concurrent_positions)` — five entries whose documented differences no longer exist — and updated the "TEN keys"/"three risk rails" header comments to match (sandbox↔live is now **eight** declared divergences, `risk.position_size_pct` the one rail still genuinely account-sized rather than min-size-sized). **Consequence for CLAUDE.md, not yet applied:** C9's "Sandbox↔live divergence is now 10 — exactly the cap" line is now stale (it's 8) — worth a pass next time that file is touched, not gating |
| C2 | **`[coa]` re-verified against Cboe** | The two keys re-checked and the provenance string updated | **Done 2026-09-11.** Re-checked against Cboe's published SPX/SPXW COA/AIM auction timing (Rule Filing SR-CBOE-2025-074): `response_window_ms = 100` (the auction response interval) and `extended_processing_ms = 900` (the NIC/matching-engine processing allowance, capped at 1000ms combined) are correct as configured. The SEC's accelerated approval of SR-CBOE-2025-074 on 2026-03-25 permanently removed the sunset provision, cementing the 900ms allowance for non-FLEX classes with no expiration — no longer the moving target the spec warned about. Both profiles' `coa.source` updated to record the re-verification and the exact approval date |
| C3 | **Blackout list regenerated for 2027** | `blackout_dates.txt` runs past the flip + 30 days | **Closed 2026-09-11 — reclassified as normal operations, not a go-live gate.** Richard re-ran `tools.blackout_dates --out blackout_dates.txt --months 12` the same day: FOMC/CPI/NFP/PCE each "run out early" against the 12-month horizon because the underlying published calendars don't extend into 2027 yet — not a bug in the tool or a repo gap. List now runs `2026-09-11` → `2026-12-24` (18 distinct dates), still comfortably past the flip and past its own 30-day amber threshold. There is nothing left to build or decide here before the flip; regeneration is periodic, source-gated maintenance that recurs indefinitely as FOMC/CPI/NFP/PCE publish further out, so it moves to normal operations (README's day-to-day rhythm) rather than staying a pre-flip checklist item |
| C4 | **NAS backup pull green streak** | `argus-pull-check.timer` reports `ok` several consecutive days | **DONE 2026-09-14 — eight consecutive `ok` days, Sep 07 → Sep 14**, read off `journalctl -u argus-pull-check.service`. Both arms green every day: newest snapshot 16 h old at each 09:00 check, and a `nas_pull_key` login every morning (~08:27–08:33). `systemctl list-units --failed` is empty — the independent second signal that does not depend on Slack having delivered — and both timers show a live `NEXT`. Retention is at its ceiling and rotating normally (14/14, 6.7 G, one file out per night). This is the streak v0.3.21 made trustworthy: it fixed the unprivileged-run misreport, so an `ok` read under `sudo` now means what it says |
| C5 | **The broker will not take a closing order — accepted as first-week risk, with a manual-close SOP** (BROADENED 2026-09-14 from "stuck `Cancel Requested`") | Written down as an accepted risk with a monitoring plan | **ACCEPTED 2026-09-14 (Richard). See C5 below** — the item was narrower than the risk, and the wider member was observed the same day |
| C6 | **CI green on `main`** | Last Actions run confirmed passing | **SATISFIED AT THE FLIP 2026-09-15** — the cutover was performed on `v0.4.2`, which was already the deployed cert image and had run that day's cert session (4 trade ideas, 1 filled, held to expiry) before the 16:30 flip. Original note: **Unverified from here** (`gh` not installed, no network). Local proxy: full suite 2114 passed at `v0.4.1`. **The flip's own version of this item is narrower than CI**: confirm `/status` reports the tag you intend to cut over on (`v0.4.1`) and that it has run at least one clean cert session — the runbook's §3 note |
| C7 | **2027 NYSE calendar verified** | The `RE-VERIFY` markers removed | **Closed 2026-09-11 — reverified against the official NYSE calendar and reclassified as normal operations, not a go-live gate.** Richard's re-check matched all 11 dates in both `argus.live.toml` and `argus.sandbox.toml` exactly: the 10 2027 holidays (New Year's, MLK, Washington's Birthday, Good Friday, Memorial Day, Juneteenth-observed 06-18, Independence Day-observed 07-05, Labor Day, Thanksgiving, Christmas-observed 12-24) and the one 2027 half-day (day after Thanksgiving, 11-26, 1:00 PM close). No divergence, nothing to fix. `# 2027 (RE-VERIFY)` markers replaced with `# 2027 (re-verified 2026-09-11)` in both files; `tests/test_config_profiles.py` still 19/19. Verifying the following year's calendar is a recurring annual task, same as the blackout list (C3) — moves to normal operations rather than staying a pre-flip checklist item |
| C9 | **`[broker] account_number` pinned, and `__main__` passes it** | A live profile refuses to start without it, and the supervisor filters to that account | **Done 2026-09-10** — built per [the design](designs/2026-09-10-pinning-the-traded-account.md); decision record [2026-09-10 — pinning the traded account](decisions/2026-09-10-pinning-the-traded-account.md). `argus.live.toml` pins `5XX00000`; `config.py` refuses `environment = "live"` without it (its own branch, firing before the staleness and audit live guards); `__main__` passes `config.broker.account_number` through the account filter the 2026-08-31 record already had; `/status` carries an Account row and `account.pinned` in the JSON; the live reconcile alert names the shared account as the likely cause when every position discrepancy is `unexpected_at_broker` — diagnostic only, bounded by test. Cert ships `""` = unpinned by decision (a pinned cert account turns a re-provision into a permanent `SessionAborted`). Sandbox↔live divergence was **10 — exactly the cap** at the time this landed; **C1 (2026-09-11) retired five now-stale `ALLOWED_DIVERGENCE` entries by syncing cert's risk caps to live's, dropping the count to 8** — `MAX_SANDBOX_LIVE_DIVERGENCE` itself is unchanged at 10, so there is headroom again, and the next divergence should be weighed against 8, not assumed to trip the base+overlay reopen on its own |
| C8 | **The tastytrade account is flat of everything that is not Argus** | The last legacy positions have resolved and the account holds nothing Argus did not open | **RE-CONFIRMED BY MACHINE 2026-09-15** — the first live `startup_reconcile` returned `ok: true` with no position discrepancies, no order discrepancies and no unmappable orders, which is a stronger read than the UI eyeball. **DONE 2026-09-14 (Richard). The last non-Argus position closed today**, under normal trade-management policy rather than forced for the flip — which is how it was supposed to end. The account is now **fully dedicated to Argus**: manual trades and the other strategies have moved to another brokerage account. This was the only item on either list waiting on the *market* rather than on a decision or a read. Why it gated everything: Argus's rails read whole-account numbers, so one foreign leg is an `UNEXPECTED_AT_BROKER` that hard-halts reconciliation at boot with `HaltGrade.ALL` (the watchdog never starts either), inflates `open_spread_equivalent` past `max_concurrent_positions`, and puts non-Argus P&L inside the breaker's NLV anchor — which is G1's first-ever exercise. **What this closure does NOT do:** it is not permanent. Flatness is a standing property re-confirmed each morning (runbook §6), kept true only by nothing else trading there. The account stays funded at minimum size; the account the other strategies moved to is separately funded, so no capital left tastytrade |

### C5 — ACCEPTED, and BROADENED, 2026-09-14 (Richard)

**The item as written was narrower than the risk it was standing for.** A stuck
`Cancel Requested` was one way for a position to become uncloseable by machine.
2026-09-14 demonstrated the other one, for four and a half hours.

**The class: the broker will not take a closing order while a position needs
closing.** Two members, and the harm is identical:

| | Seen? |
|---|---|
| A cancel accepted and stuck at `Cancel Requested`, so `_await_settled` never gets its positive confirmation and the cross is refused | **Never** — and as of the 2026-09-20 gate read, **live has sent no cancel DELETE at all**: all four live walks filled at step 0. The benign fast case is seen in CERT only — two P5 residuals terminal within 1.5 s |
| **Submissions failing outright** — the send never lands, so nothing exists to confirm | **YES, 2026-09-14, 11:33 → 15:30 ET.** `walk.disconnected` × 7, `order.submit_failed` × 4 |

**What 2026-09-14 showed.** `20260914-call-7625-7630`'s credit stop fired at
11:33:10 against a 1.60 mark on a 1.40 stop. Every send failed at the transport
layer. The exit attempt was spent at 11:34:56; the 15:30 flatten fired to the
second, claimed its one-shot override, and died the same way at 15:30:12. The
position rode to settlement. **Argus was correct at every step** — a retry IS the
double-close, which is why the watchdog will not do it — and the position was
uncloseable by machine with $860 of width standing behind it.

**Accepted as first-week risk**, on three grounds: it is bounded by the spread
width and therefore by `max_loss_per_position`; the machine's refusal to retry is a
deliberate rail, not a defect to be softened; and the residual exposure is covered
by a human, which is what the SOP below is for.

**The monitoring plan, and it is one signal.** `watchdog.exit_spent_still_held`
pages repeatedly on the existing `watchdog.exit_unattempted_max_s` escalation —
527 records over 4h25m on 2026-09-14, arriving as roughly **one Slack alert every
15 minutes** through B1's `(kind, scope)` 900 s per-position throttle. That page is
the signal, and there is no second one.

**The SOP: when `exit_spent_still_held` pages, close the position by hand.**

1. Read `/status` — the held row now carries `EXIT SPENT (<outcome>)` and a live
   mark (`tools.desk` likewise, fixed 2026-09-14). Confirm the position is still
   held and still triggered.
2. **Check whether anything is still working at the broker** before sending
   anything. If a closing order is live, cancelling it in the app is the first
   step — a second close on an order that can still fill re-opens the position
   inverted on expiry day, which is the exact harm the machine's one-attempt rule
   exists to prevent, and the human inherits it. **Since the 2026-09-25 build the
   page and `/status` NAME a resting cross** (`working_close`: order id, limit, age;
   "CLOSE WORKING at the broker"). When that line is present, a close IS working, and
   the answer is usually to leave it: a far-OTM cross that has not filled is resting
   against a book with no bid on the long leg.
3. Close the spread in the tastytrade app as one multi-leg order. **Never leg it.**
4. Expect reconciliation to see it: Argus's `positions` will disagree with the
   broker until the fill books, and the `missing_at_broker` path is the expected
   resolution, not an alarm.

**Added 2026-09-24, for the stub-close build: the SOP's step 2 is now load-bearing
in both directions.** A partial close's stub gets ONE automated further close
(`watchdog.stub_close`, seconds after the partial's fill BOOKS, through the guard's
new quantity rule) — so by the time a human reads the page, Argus has usually already
acted, and the spent page's `stub_close` field says which way: `available` means
Argus is still going to try (a refused ask retries one refresh interval later, and
past 15:30 the flatten goes first), `sent`/`exhausted` means the page and the 15:30
flatten are what is left. The race the SOP's step 2 exists for also runs the other way now: an operator
who closes the stub by hand while Argus still counts it held leaves the guard facing
a flat book on the next ask — the dedicated-account non-negotiable and the
"check the broker before sending anything" step are the mitigations, and since
2026-09-24 the spent page names the stub state so the operator knows Argus still
counts the position as held.

**Two outcomes are NOT this risk and must not be treated as it.** `NOT_ATTEMPTED`
(no order could be built — the flatten's modal regime, since a winning 0DTE spread
is nearly worthless by 15:30) does not spend the attempt and is not a stuck
position. And `NOTHING_AT_BROKER` / `CROSS_UNPRICEABLE` retry under
`_EXIT_SEND_ATTEMPTS`, so a page from either may resolve itself — check the budget
and the flatten allowance before acting.

**What would retire this, and it is not a build.** A confirmation-gated,
per-outcome exit re-attempt is an open decision (§3), deliberately not taken. The
cheaper retirement is evidence: if live's order path does not exhibit cert's
transport failures over the first weeks, the observed member of this class goes
away with the environment that produced it. The stuck cancel stays unseen either
way.

→ [the desk view hides a spent exit](decisions/2026-09-14-the-desk-view-hides-a-spent-exit.md)

**The kill-side mirror has its own SOP, added 2026-09-20, and it lives in `README.md`
beside the kill-switch endpoint** (*"you pressed `/kill` and an entry order may still
be working"*). C5 is about a position the machine cannot CLOSE; that one is about an
opening order the machine cannot CANCEL — the same shape one step earlier in the
lifecycle. Since v0.4.5 the machine re-asks for as long as the broker says the order
is working, so the SOP covers only what P5 cannot reach: **the registry never
rehydrates**, so any restart after a kill orphans the order (the next boot's
reconciliation halts entries and pages, but cancels nothing); `/kill` itself may be
unreachable, in which case the broker's app is the only kill switch there is; and an
order whose ack was lost and which could not be read afterwards has no id the machine
can cancel. The end-to-end assertion that everything *inside* that boundary works —
press, first cancel fails, second cancel lands — is
`tests/test_kill_reaches_a_working_order.py`.

**Verified and needing nothing (2026-09-10):** DO host monitoring — four alert
policies exist at 70% (CPU, memory, disk, 5-min load), droplet disk 13 G used of
77 G (17%), of which the 14 retained snapshots are 6.9 GB. The disk alert covers
backup growth. NAS has 3.4 T free against 50 archived snapshots.

---

## 3. Backlog — post-go-live

Nothing here gates the flip. Eventually its own document.

### Parameters awaiting data that only live can produce

- **`execution.walk_source` / `watchdog.trigger_source` — a scaling gate, not a
  go-live one.** All nine values carry UNCHANGED from the 2026-08-10 set as a
  *recorded* live-min-size decision (2026-09-09): none is denominated in account
  size, so the `max_loss_per_position` 1250 → 950 change does not reach them. The
  live strings diverge from cert's for the first time and state why cert cannot
  tune them (the undocumented multi-leg fill mechanism; entry-side, the path
  stopped since 2026-09-02 by the lost 5-point ladder). `trigger_source` widened
  from three keys to five to cover the exit walk, giving `exit_walk_away_pct = 0.20`
  its first written justification (the entry's 0.10, doubled).
  **The pass is owed at the first size increase**, gated on baselining the
  `walk.finished` distribution first. `entry_drift_abort_points` is the one
  parameter whose data is **already admissible** — it is about the underlying's
  path, not fills — with its method fixed (time-of-day-conditioned 20–30 s move
  distribution + the 07-29 and 08-03 replays) and **not yet run**.
- **`watchdog.exit_cancel_settle_s = 3.0`** is still an unmeasured guess in all
  three profiles. One incidental data point: two P5 residuals went terminal within
  1.5 s. Set it from a real submit-then-cancel measurement.
- **`CONFIRMED_IN_MARKET = frozenset({OrderStatus.LIVE})`** — which other statuses
  mean "in the market" is unmeasured. Widen only as a measurement, off a real
  order's `AlertStreamer` transitions during RTH.
- **Tier-2 audit queue bound (`10000`) / alert depth (`7500`)** never sized from
  observed volume. Today's depth is 0 with 0 dropped, so nothing is near it.

### Known gaps left under closed work

These are real and were buried in the "what's needed" column of closed rows.

- **An UNWATCHABLE position gets no flatten.** `/status` names it now, but
  `ExitExecutor.close` needs the `OpenSpread` that could not be built, so
  `FLATTEN_BEFORE_CLOSE` cannot fire for it. On 0DTE it rides to settlement.
- **Residual orders never rehydrate across a restart.** P5 prevents the state from
  arising; it does not resolve a previous process's orphan.
- **One failed `get_live_orders()` poll blinds two readers** — P4's working-order
  count and P5's residual recheck — and takes the `/status` residual row quiet with
  it. Deliberate and documented at
  `watchdog.py:3436` (private record, not published) (the count only ever
  gates new entries, so a read blip must not halt the watchdog). Worth noting how
  often it fires: **52 `watchdog.working_orders_unread` records in one cert session**
  (2026-09-10).

### Open decisions

- **The deploy's post-restart read-backs cannot see a crash loop. Design owed.**
  `argus-deploy.sh` asserts the running version and the config fingerprint by
  grepping the journal — so a process that dies *after* the startup banner writes
  the banner again every cycle and both read-backs pass, while `restart:
  unless-stopped` keeps compose "active". **Narrowing from the original write-up:**
  the specific example it named (a `.env` missing a credential slot) *is* caught —
  the script's `RED_FLAGS` grep includes `Traceback` and `refusing to start`, and an
  unhandled `MissingCredentialsError` from `session.py:606` prints both. The class
  stands for any crash printing none of those strings. The fix is an assertion a
  DEAD process cannot satisfy — a `GET /status` after the restart, or a liveness
  re-read a few seconds later — not a second journal grep. Same class as v0.3.21's
  NAS check, and it recurred the same day one layer over: `docker compose run` reads
  `env_file` into a NEW container, so verifying a credential that way says nothing
  about the RUNNING one (`docker exec` is what answers it). **One rule: a check
  performed in a fresh context proves nothing about the live one.**
- **No entry back-off when the broker refuses everything.** Bound on the *count* of
  consecutive terminal rejections, never on `reject_reason` text. **Corollary from
  v0.3.15:** such a counter must never count a `'Received a fill on the original
  order'` rejection — "your order filled" is not a broker refusing to trade, and
  that walk now records `FILLED` and never reaches a counter.
- **DigitalOcean droplet snapshots** — deferred with Litestream, still undecided.
  The 2026-09-06 NAS re-audit is evidence *for* enabling them: the single off-box
  copy was silently not being produced for two days, caught only because someone
  looked. It is monitored now, but it is still one copy, on one consumer-grade
  device, whose recovery step is manual after every power event.
- **NAS retention to 30 days** — deferred past go-live (Richard). The NAS prunes
  nothing today; the droplet's 14 files are a staging window, not the policy.

### Structural, accepted, not worth building around

- **Blackout list regeneration is recurring, source-gated maintenance, not a
  one-time task (moved here from C3, closed 2026-09-11).** `blackout_dates.txt`
  can only ever cover as far out as FOMC/CPI/NFP/PCE have themselves published —
  the 2026-09-11 re-run confirmed the list still stops at `2026-12-24` on a
  12-month horizon, because the sources haven't published 2027 dates yet, not
  because of anything to fix in `tools.blackout_dates`. `/status`'s amber-at-30-days
  warning already covers the operational risk; the runway just gets regenerated
  and re-deployed whenever it gets short, same as any other day-to-day task in
  README's rhythm.
- **NYSE calendar verification is recurring, annual maintenance, not a one-time
  task (moved here from C7, closed 2026-09-11).** Observed-date rules shift with
  the weekday a holiday falls on, so each new year's closures and half-days need
  a fresh check against the official NYSE calendar before the repo can trust
  them — 2027 was reverified 2026-09-11 (all 11 dates matched, no divergence);
  2028 will need the same pass once it matters. Same shape as the blackout list
  above: a runway that runs out on its own schedule, not a defect.
- **One production app registration serves both live trading and live market data.**
  Surfaced by `credcheck` on 2026-09-10: the two LIVE slots hold the same
  `CLIENT_SECRET`, different refresh tokens. Legitimate — scope rides on the token,
  not the app, so a `read`-only market-data token beside a `read`+`trade` one is
  real broker-enforced separation from one registration. **Two consequences to know
  rather than to fix:** revoking that registration takes market data and order
  routing down together, and the read-only / money-moving split then holds only in
  code (`SessionRole`, where `tt.trading_session` RAISES on a market-data session),
  not in the credential. Splitting into two live registrations would harden it;
  nothing requires that before the flip.
- **The NAS crontab re-add is manual after every power event.** Found *missing*
  2026-09-06 with the daemon running unsupervised. Nothing can change this.
- **Nothing verifies that a repo file whose deployment target is the NAS arrives.**
  v0.3.20's NAS-side hardening sat undeployed in the droplet's checkout until it was
  copied by hand — `argus-deploy.sh` touches one host and the classifier is correct
  about the container and silent about the third machine.
- **The deploy classifier must learn any new bind mount.** Add a mount to compose
  without teaching the classifier and that file's release deploys with no restart
  and no error. Guarded: `test_nothing_compose_bind_mounts_is_treated_as_inert_by_the_deploy`
  passes (18/18 in `tests/test_deploy_wiring.py`). **This is a standing landmine,
  not a task** — it belongs in CLAUDE.md's landmine list.

### Cosmetic / informational

- `GET /kill` → 405 while `POST /status` → 404. Tidy if touching `control.py` anyway.
- Whether tastytrade re-delivers order state on re-subscribe. The broker-agreement
  check was built so the answer does not matter; informational only.

---

## Could not verify from here

Not "checked, fine". This session had no droplet and no network.

- **CI green on `main`** — `gh` not installed, no network. Local suite passes.
- **`argus-pull-check.timer` has reported `ok` on consecutive days** — needs a
  droplet read (C4).
- **`[server] bind_host = "192.0.2.10"` is the correct Tailscale IP** — unchanged
  in both profiles, and the 2026-09-10 `/status` curl against it succeeded, which is
  as close to verified as the repo gets.
- ~~**`/etc/argus/host.env` exists on the droplet with the right profile**~~ —
  deliberately not in git, but **read directly on 2026-09-15 during the cutover**:
  it held `ARGUS_ENV=sandbox`, was changed to `ARGUS_ENV=live`, and the running
  process's config fingerprint confirmed the profile took.
- **`daily_pnl` behaviour against a real marked NLV** — cannot be exercised anywhere
  but live. This *is* G1. **Still unexercised as of 2026-09-15**: the flip happened
  post-close, and the first live session (2026-09-16) is an FOMC blackout on which
  Argus holds nothing, so the rail has no input to move yet. First real read is the
  first day with a position on.

---

## Closed — for the record

One line each. Narrative in `docs/decisions/` and `docs/releases/`.

| Finding | Closed by |
|---|---|
| Kill switch does not survive a restart | **v0.3.14** — rehydrated from the audit log, no date bound, `POST /resume?confirm=yes` the only clear, fails closed to ENGAGED |
| `_end_day` stops the watchdog on a live position | **v0.3.14** — `_end_entries_today` + the session-scoped `EntriesEnded` latch |
| `OpenSpread.entry_credit` is the LIMIT, not the fill | **v0.3.15** — contracts-weighted realized price, migration 0005. The `credit_multiple` prohibition NARROWED: only `entry_credit_basis = realized` positions may support a conclusion |
| `/status` shows nothing for an UNWATCHABLE position | **v0.3.15** — the `unmanaged` section's first row (residual gap in §3) |
| `/status` does not name a residual order | **v0.3.15** — the same section's second row, carrying the recheck MODE (residual gaps in §3) |
| No running-build-version surface | **v0.3.15** stamp, **v0.3.16** deploy read-back, **v0.3.17** config fingerprint. `/status` reports `v0.3.22` |
| Live trading OAuth app registration + credentials | **CLOSED 2026-09-10 (Richard).** Both halves. **Repo side, v0.3.22 (2026-09-09):** credentials sit on a `(ROLE, WORLD)` grid — `TASTYTRADE_TRADING_CERT_*`, `TASTYTRADE_TRADING_LIVE_*`, `TASTYTRADE_MARKETDATA_LIVE_*` — so a `live` profile reads its own slot instead of one unprefixed pair, refuses to start with no live slot or with one holding the cert values, and `/status` names the slot in use. That is what stopped the cutover from secretly being a hand-swap of `.env` VALUES on top of the `host.env` flip; rollback is now symmetric. **Provisioning side:** done, and done since live market data went in. **VERIFIED IN THE RUNNING CONTAINER 2026-09-10** — `sudo docker exec argus python -m argus.tools.credcheck` after a `systemctl restart`: all three slots present and complete (cert trading secret 40 / refresh 586, live trading 40 / 547, live market data 40 / 539), *"no shape problems found — every slot present is complete, and no live slot is carrying a cert value."* **`docker exec`, not `docker compose run`**, which is the instrument that answers the question — a `compose run` reads `env_file` fresh into a NEW container and says nothing about the environment the RUNNING one was created with. (`.env loaded: False` there is correct and not a finding: inside the container the variables arrive from compose's `env_file`, so there is no `.env` at that path to load.) **One property outlives this row and is not a gap in it:** nothing authenticates the live registration until the cutover itself — sequenced as a step inside G3 rather than tracked as its own gate |
| A watchdog exception is `HaltGrade.ALL` and sets the shared stop event | **CLOSED 2026-09-10 — designed, signed off and built the same day.** A watchdog exception now engages the kill switch at `HaltGrade.ENTRIES` and **keeps watching**: entries stop immediately and permanently, exits and the 15:30 flatten run on. **The finding understated it** — the halt took the watchdog down three independent ways and the third (`run()` returning) was in no write-up, so grading alone would have left a dead watchdog inside a process reporting `running`. Per-position isolation, a crashed exit task now spends the attempt (`NOT_RESOLVED` + a `crash_error` sentence), `_reap_exits` in a `finally`, `AuditWriteError` the one exclusion keeping `ALL` verbatim, and a three-state `/status` Watchdog row. `src/` only, no config key; suite 2113. → [decision](decisions/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md), [design](designs/2026-09-10-a-watchdog-exception-must-not-take-the-watchdog.md) |
| Order DIRECTION provenance on the exit path | **v0.3.16** — mint over infer. The generating question was answered by observation: tastytrade **relabels**, it does not reject |
| `unmanaged_order_cancel = true` in `argus.live.toml` | **v0.3.17** — `false` in live and dev; **promoted to `true` in cert** (`argus.sandbox.toml:714`), which was config-profiles step 3 |
| `unmanaged_order_cancel` still `false` in live (the post-cutover promotion) | **v0.4.5** (deployed 2026-09-20) — promoted to `true` in live. The cert-first condition was replaced because cert has had no entry path since ~2026-09-02; the gate became a read of live's own `order.residual_*` rows, **read 2026-09-20 on the 2026-09-19 snapshot: ZERO residual rows, and zero cancel records of any kind — all four live walks filled at step 0, so nothing has ever walked away.** The promotion therefore rests on the structural argument, and the claim that the DELETE was already proven live is withdrawn. → [decision §4.1](decisions/2026-09-19-live-cancel-recheck-promotion.md) |
| Litestream → R2 replication not built | **Decision 2026-09-04** — DEFERRED PAST GO-LIVE on a cost litmus test, not merit. A lost tail cannot cause a wrong trade. Runbook §15 corrected 2026-09-07 |
| DO host-monitoring alerts / disk headroom | **Verified 2026-09-10** — four 70% policies exist; disk at 17% |
| `replace_order` on a partially-filled order | **Measured 2026-09-01/02** — it carries the REMAINDER. Nothing owed; current code still cancels the remainder, which is safe |
| `WalkOutcome` exhaustiveness not mechanical | **Was never open.** `test_the_outcome_table_is_the_whole_enum` (`tests/test_entry.py:381`) asserts `covered == set(WalkOutcome)`, added 2026-08-04 — a month before the audit, which inherited a stale claim from `docs/architecture/00-index.md:123` |

---

## Documentation fixes — APPLIED 2026-09-14

One, found while updating this file and the cutover runbook for C8's closure. It
is the same pattern the 2026-09-10 batch below records, so it goes here rather
than into a finding.

- **`docs/live-cutover-runbook.md` §1 stated an NLV floor that does not exist.**
  The `position_size_pct` row read *"implies an NLV floor of 950 / 0.03 =
  $31,667 — below it nothing trades,"* which divides `max_loss_per_position` by
  `position_size_pct`. Those two rails are applied to **different quantities**:
  `PreTradeGate` bounds the *proposal's* defined risk
  (`contracts × (width − credit) × 100`) by `position_size_pct × NLV`, and
  `size_contracts` floors at one lot so the gate is what refuses. The real floor
  is **graded and depends on the candidate**: ~$15,000 for one 5-wide lot,
  ~$30,000 for two, and $31,667 only for the *widest* candidate `max_width`
  admits. Corrected as the runbook's new **§1.2**, and P7 downgraded from a hard
  precondition to a sizing expectation. **Consequence if it had stood:** a live
  account somewhere between $15k and $31k would have read as "below the floor,
  do not flip" when Argus would in fact have traded it correctly at reduced size —
  and, the other way, `size_cap_exceeded` on wide candidates would have read as a
  defect on day 1 rather than as the rail working.

## Documentation fixes — APPLIED 2026-09-10

Found during this re-verification: each was a false statement someone would read as
true. All fixed; recorded here because the *pattern* is worth carrying.

- **`docs/architecture/00-index.md`** — item 10 (`WalkOutcome` exhaustiveness "not
  mechanical", false since 2026-08-04) and item 9 (Litestream "not built" with no
  deferral decision). Two more in the same list were the same defect and were fixed
  alongside: item 3 (`replace_order` semantics "unmeasured" — measured 2026-09-01)
  and item 6, which described `walk_source` / `trigger_source` as reading
  `"UNSET PLACEHOLDER"` **in `argus.toml`, a file that no longer exists**.
- **`docs/releases/README.md`** Notes table — the `*v0.3.17 – v0.3.20 — rows owed*`
  placeholder replaced with four real rows, a v0.3.22 row added, and the
  v0.3.14 / v0.3.16 rows corrected from "NOT YET TAGGED" against tags that exist.
- **Release-note headings** — `v0.3.10.md`, `v0.3.11.md` ("DRAFT. Not yet tagged."),
  `v0.3.16.md` ("NOT YET TAGGED") and `v0.3.22.md` ("PREPARED, not yet tagged or
  deployed") all corrected. Every one of them was tagged and shipped.
- **`docs/argus-deployment-runbook.md` §15** — *"Unbuilt: order execution, position
  watchdog"*, a shadow-phase line that outlived its phase by about a month.

**The pattern, and it is the same one the NAS pull taught on 2026-09-06: a document
that is correct when written goes false silently.** Nothing in this repo fails when
a release doc still says "not yet tagged" after the tag exists, or when an
architecture index describes a config file that has been deleted — and the go-live
audit *inherited* one of those stale claims (`WalkOutcome`) and carried it as an
open finding for a week. **A release-note heading and the index row for it are two
copies of one fact; tagging updates neither.** Worth a `git tag` hook, or a test
asserting no `docs/releases/v*.md` heading claims "not yet tagged" for a version
that has a tag — the same shape as `test_deploy_wiring.py`'s assertions, which is
the only reason the deploy's own prose has not drifted the same way.
