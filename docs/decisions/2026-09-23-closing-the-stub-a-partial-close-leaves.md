# Closing the stub a partial close leaves (2026-09-23)

> **RESOLVED 2026-09-24 — Candidate A (the quantity-aware guard) plus its
> watchdog half are DESIGNED AND BUILT, and TAGGED as v0.5.1; DEPLOYED, at the latest, with v0.5.2 (2026-09-26 — this record carries no separate v0.5.1 deploy date).**
> [Design](../designs/2026-09-24-quantity-aware-guard-for-close-identifiers.md),
> built per its §10 sequence with the §9 suites (`tests/test_order_guard.py`,
> `tests/test_stub_close.py`, the two deliberate profit-target updates), and
> revised the same day for the twelve findings of its code review (design §12 —
> among them: the guard rule now only clears a filled-close refusal and rides the
> same switch, the continuation needs BOOKED evidence and never pre-empts the
> 15:30 flatten, and any guard refusal restores the allowance, paced). The
> §6 gate below is **DISCHARGED for the profit target's promotion**: what gates
> `profit_target_enabled = true` in live now is only the promotion's own
> judgement, not this record. Candidate D (walking the remainder) remains
> complementary and still gated on its own live measurement. Originally:
> **OPEN DECISION — SPEC, DESIGN AND BUILD OWED. NOTHING HERE IS BUILT.** Raised by
> Richard 2026-09-23 from the profit-target review ([§12.1 of the profit-target
> record](2026-09-22-profit-target-and-re-entry.md)). This record states the problem,
> sizes what happens if no human acts, lists the candidate fixes and the acceptance
> criteria a fix must meet. It decides nothing. **Recommended gate: resolve this — at
> least candidate D below — before `watchdog.profit_target_enabled` is promoted to
> `true` in live** (§6).

## 1. The state

A closing order for an N-lot spread fills PART of the position. The walk cancels the
remainder (`loop._resolve`: *"partial fill; remainder cancelled"*) and reports
`FILLED`. What is left is a **stub**: fewer contracts of the same vertical, still held.

**No automated close of that stub can ever be sent.** Every close of a position uses
the same identifier, `argus-close-<candidate_id>`, and `DuplicateOrderGuard` BLOCKS on
a FILLED order under the identifier it is asked about — correctly, for the case it was
built for, since a second close sized from a ledger that has not booked the first fill
yet would over-close and re-open the position inverted. So the stub's stop, its
backstop and its 15:30 flatten override all come back `BLOCKED`.

Two paths reach it, and one is new:

| path | since | stub starts as |
|---|---|---|
| emergency exit (credit stop / backstop / flatten) fills partially | always | a position that already hit its stop, or is being flattened |
| profit take fills partially | v0.5.0, only once `profit_target_enabled = true` | a near-worthless winner (mark ≈ 0.05) |

Since the 2026-09-23 review both are handled the same way: the fill spends the
position's close (`_spend_attempt`), so the watchdog stops trying and **pages**
instead of sending an order the guard will refuse.

## 2. What happens if no human acts

Traced through the code as it stands after the review fixes, at live minimum size
(`max_contracts_per_position = 2`, so a partial leaves exactly **1 contract**):

1. **Watched, never closed.** The stub stays in the scan: evaluated and marked every
   tick (`watchdog.check_spent`), its live mark on `/status` as **NO EXIT LEFT**.
2. **Paged, repeatedly.** `watchdog_exit_spent_still_held` fires every
   `exit_unattempted_max_s` (30s) with the mark; Slack's 900s throttle folds the
   repeats into `(+N suppressed)`. This page is the trigger for C5's manual-close SOP
   (`docs/go-live-findings.md`, pointed to from `README.md`).
3. **No new entries for the rest of the day.** The stub is a held spread, so it fills
   `max_concurrent_positions = 1`; the gate denies every candidate as
   `concurrent_limit`. The loss budget also counts its full width as open worst case.
   This is the protective side effect: an unattended stub cannot be followed by fresh
   risk.
4. **The 15:30 flatten does not save it.** The flatten override sends one close
   through the guard, which blocks on the FILLED order — `NOT_RESOLVED`, one
   `watchdog_flatten_override` page.
5. **It rides to PM settlement.** SPXW is European and cash-settled: no assignment, no
   pin risk, no position into the next day. The worst case is the vertical settling
   at full width.
6. **Next session is clean.** Reconciliation resolves an expired 0DTE row as expected
   (the expired-positions stand-down), so the stub does not halt the next day.

**The loss is bounded, single-day and single-contract**: at most
`(actual width − that contract's entry credit) × 100`, i.e. **≤ $450 on a 5-wide
spread** (entry credit ≥ `min_credit` 0.50) and **≤ $950 on a 10-wide**
(`max_width = 10.0`). It never exceeds what the full position could have lost, so no
size rail or the `max_concurrent_positions × max_loss_per_position ≤ daily_max_loss`
invariant is breached, and the breaker's accounting stays true.

**How likely the worst case is, split by path — both unmeasured in live:**

- **Partial fills.** All four live walks read on 2026-09-20 filled at step 0 and left
  no residual order; partials have been observed only in cert (and cert fills are not realistic). The
  rate is unknown.
- **Profit-take stub.** It starts about one tick from worthless, far OTM. The
  profit-target record measured 2 of 25 triggered positions later needing their stop
  (§11.2, n tiny); going on to settle at full width is rarer again. Low probability,
  bounded size.
- **Emergency stub.** It starts where the stop fired — already at ~2× credit and
  closer to the money. Materially more likely to get worse than a profit stub. **This
  exposure exists today, with the profit target off.**

## 3. Why the fix is not a one-liner

- **Loosening the guard** is the tempting fix and the dangerous one. Its FILLED rule
  is what stops an over-close while the ledger lags the broker by the measured
  4–5s; any relaxation has to compare QUANTITIES, from sources that can each be
  stale or unreadable.
- **A fresh identifier per close** (`argus-close2-…`) breaks attribution:
  `parse_external_identifier` takes everything after `close-` as the candidate key,
  and per-trade-idea holdings, reconciliation and the ledger all key on it.
- `filled_contracts` is rung-scoped (a known under-count), so a walk cannot reliably
  say whether its own fill was partial. The ledger and the broker's position are the
  authorities on what is held.

## 4. Candidate designs

**D — walk the remainder instead of cancelling it (smallest; removes most cases).**
The loop cancels a partial's remainder because *"what tastytrade's replace does to a
partially-filled order … has not been measured"*. **It has since been measured:
`replace_order` carries the REMAINDER** (2026-09-01: original filled 1, replacement
filled 1, position held 2). For a CLOSE, repricing the remainder inside the same walk
closes the stub under the same identifier, before the guard is ever asked. It does
not help a remainder that then walks away or times out, so it reduces the state and
does not remove it. It needs a live measurement of replace-on-partial for a
multi-leg CLOSE first; the entry-side measurement is from cert.

**A — a quantity-aware guard for CLOSE identifiers.** Permit a close of `q`
contracts iff nothing is WORKING under the identifier, every order is readable, and
`sum(filled close quantity) + q ≤ opened quantity`, with the opened quantity from the
opening order and `q` from what the ledger holds only once it has booked every
closing fill. Fails closed on anything unreadable. Most general; touches the one
duplicate protection in the system, so it needs its own design pass and mutation
tests.

**B — size the stub's close from the broker's position**, not the ledger's, via REST
at send time, with the guard relaxed only for a CLOSE whose size equals what the
broker reports held in both legs. Same fail-closed requirements as A, plus the
chained-vertical hazard: shared strikes net to zero at the broker, so a symbol-keyed
read is exactly what the overlapping-verticals rule forbids.

**C — generation-suffixed close identifiers.** Rejected for the reason in §3 unless
the identifier grammar is redesigned end to end.

## 5. Acceptance criteria for any fix

- **Never over-close.** An inverted position is worse than the stub. No order may
  close more than is provably held, and a lagging ledger must never be the proof.
- **Never double-close.** Nothing may send while any order under the identifier could
  still fill.
- **Fail closed** on every unreadable input: the stub stays paged and unsent.
- **Survives a restart** (a stub held across a crash must not get a fresh budget).
- **Bounded**, one further close attempt per stub, on the flatten-override precedent.
- **Tested end to end on the real executor and guard with a partial fill**, and the
  existing `test_the_guard_really_blocks_a_close_after_a_partial_take` updated
  deliberately rather than deleted.
- **Measured first:** replace-on-partial for a multi-leg CLOSE, in live, before D.

## 6. Follow-up

- **Owner:** Richard. **Status:** resolved 2026-09-24 (built; tagged v0.5.1; deployed at the latest with v0.5.2, 2026-09-26).
- **Gate:** ~~do not set `profit_target_enabled = true` in live until D is
  built, or this risk is accepted in writing here with §2's numbers~~
  **DISCHARGED 2026-09-24** — Candidate A is the "at least" the gate named and
  more (§7.7 of the design): it closes the stubs that remain from ANY path,
  including D's own leftovers, and needs no replace-on-partial measurement.
- **Evidence to watch now** (read-only, live DB): any `walk.finished` for an
  `argus-close-*` identifier with `reason` containing `partial fill`, and any
  `watchdog.exit_spent_still_held` with `exit_outcome: filled`. Either one is a real
  stub; the first sighting also sizes the partial-fill rate.
