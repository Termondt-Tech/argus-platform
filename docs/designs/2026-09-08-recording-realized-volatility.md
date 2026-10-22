# Design — recording realized volatility

**Status: SHIPPED 2026-09-08.** Approved with all nine §9 decisions as written, no
amendments, and built the same day. The normative record is
[`docs/decisions/2026-09-08-recording-realized-volatility.md`](../decisions/2026-09-08-recording-realized-volatility.md).

**One implementation-level finding this document did not anticipate**, recorded in
the decision: §3.7's coverage gate fired on its first real run and **invalidated
two sessions the measurement that motivated this design had included** — most
importantly 2026-09-03, whose span is 359 of 360 minutes and whose coverage is
0.24, because the 11:04 halt to the 15:37 restart leaves a 273.9-minute hole. It
was that study's only counterexample. §3.7 argued for two separate metrics on
principle; this is the case that shows one would not have been enough. Gated,
every conclusion of the motivating study strengthens.

This file stays as the record of what was *rejected* and why.

---

**Status when written: PROPOSED 2026-09-08, awaiting sign-off.**

Origin: the 2026-09-08 planning pass over
[the first measurement from Argus's own record](../../RESULTS.md).
That pass produced two candidate pieces of work. One — vol-scaling `signal.width`
— was **killed by its own go/no-go test** (study 2, null once time of day is
controlled). This is the other one, which the measurement left standing.

Related open item: `docs/post-close-outcome-processing.md` (private record, not published)
poses the same "at close in-process, or at next start, or as a tool?" question for
item 1. §3.2 here answers it for this feature only and does not bind that one.

---

## 0. What this changes, in one paragraph

`signal/regime.py` estimates **implied** vol on every tick. Nothing computes
**realized** vol, so `atm_iv` is a level with nothing to be a level *against* —
which is the difference between knowing how far the market will move (measured:
r = 0.84) and knowing whether the premium is rich. That gap is
`gate-signal-candidates.md`'s Tier 1 candidate #2 and it is the one conditioning
variable the 2026-09-08 measurement left genuinely open. This adds a **pure
realized-vol estimator** and the ability to read the price path back out of the
audit log — **no new config key, no new audit category, no write path, and no
change to any running code path.** It is an analysis capability plus the shared
definition a future consumer would use, deliberately not a live signal.

---

## 1. Recommendation in one paragraph

Add `src/argus/signal/realized.py` — a pure function over a price path, mirroring
`regime.compute_regime`'s no-I/O contract and sharing its `MINUTES_PER_YEAR` so
the two numbers are comparable by construction. Add a path reader that extracts
`underlying_price` for a date range from the audit log. Surface both by
**extending `tools.regime`** rather than adding a second tool, because the useful
output is implied and realized *together* and the two already read the same
records. **Do not add a `marketdata.realized_vol` audit category** (§3.3): the raw
path is already durable in the log, the computation is deterministic, and the
regime precedent is that a derived quantity rides on the record of the decision it
informed rather than being written standalone. Backfill is free and immediate —
the path runs back to **2026-07-24, 30 sessions, 27 with a full 10:00→close
window**.

---

## 2. What is actually true in source

**2.1 The price path already exists and is complete.** `orchestrator.evaluate_once`
attaches `underlying_price` to `signal.none` (Tier 2) and `signal.candidate`
(Tier 1) on every tick that has a chain, at `signal.cadence_s = 5.0`. Measured on
the 2026-09-07 snapshot: **100% of those records carry a price**, 30 sessions from
2026-07-24, 27 spanning 10:00 to ~15:59:57. `signal.skip` (no chain) carries no
price and is correctly absent.

**2.2 Tier 2 records are droppable, and it does not matter here.**
`AuditLog.telemetry` returns `False` and increments `_dropped` when the bounded
queue is full ([audit/log.py:162](../../src/argus/audit/log.py)) — a deliberate
fail-closed choice over unbounded growth. So the recorded path can be *sparser*
than 5s under burst. Quadratic variation tolerates that: the measured volatility
signature plot is **flat from 5s to 900s** (0.118–0.128), so a grid that loses
some points is still consistent. What it would *not* tolerate is a truncated
window, which is §3.1's whole argument.

**2.3 There is a sanctioned in-process read path.** `AuditLog.read(fn)`
([audit/log.py:361](../../src/argus/audit/log.py)) runs a read-only query on the
writer's own connection, serialized behind `_chain_lock`, explicitly so
in-process consumers do not open a second connection. `risk/rehydrate.py` and
`risk/kill_state.py` already read state back from the log this way, and
`rehydrate.py` argues on the record against a new table for exactly this class of
state. **Reading the log back from the session is an established pattern, not a
new coupling.**

**2.4 Date-bounded category lookups are indexed.** Migration
`0003_audit_status_index.sql` indexes `(category, ts)` so day-bounded reads stay
flat in total history — the same index the breaker rehydration relies on.

**2.5 One writer, enforced.** `db/lock.py` takes an exclusive `flock`. Any tool
that *writes* to the live DB needs the service stopped, which is why
`tools.mark`'s write is a stop-the-service step and its `--dry-run` is not. This
is load-bearing for §3.3.

**2.6 The annualization convention is already fixed.**
`regime.MINUTES_PER_YEAR = 365 * 24 * 60`, matching CBOE's own convention. Any RV
that is going to be divided into an `atm_iv` must use the identical basis.

---

## 3. The design questions

### 3.1 (Q1) The path comes from the AUDIT LOG, not an in-memory accumulator

Two shapes:

**(a) In-memory accumulator.** The session loop already sees every chain
snapshot; accumulate log-returns in memory and emit at close.

**(b) Read the recorded path back.** Query the day's `signal.*` records and
compute over them.

**Recommend (b).** The deciding argument is restarts. Argus restarts inside a
session — deploys do it, and 2026-09-03's cert-502 halt did it at 11:04. An
in-memory accumulator that comes up at 13:00 computes RV over the afternoon only
and **labels it as the day's RV**, with nothing in the number saying so. That is
the silently-wrong failure class this codebase consistently refuses. The recorded
path has the whole day regardless of how many processes wrote it.

Second argument: **(b) makes backfill and live the same code path.** 27 historical
sessions become a correctness test of the production code rather than of a
one-off script.

The case *for* (a) is 2.2's droppability — an accumulator sees every tick. Per
2.2 that is the tolerable defect and a truncated window is not.

### 3.2 (Q2) It runs as a TOOL now, not in-process

Three shapes: in-process at session end; in-process at next session start
(rehydrate-shaped); a standalone read-only tool.

**Recommend the tool**, and defer the in-process version until a live consumer
exists.

- **Nothing reads trailing RV yet.** Building a session-end phase for a consumer
  that does not exist is speculative work in the most delicate part of the
  process — session teardown is where `_end_day` took the watchdog down with it.
- **A tool cannot hurt the trading process.** No config key, no loop cost, no
  halt path, no deploy coupling.
- **A missed run costs nothing**, which is the argument that defuses the usual
  objection to human-scheduled telemetry. Because the raw path is durable in the
  log and the computation is deterministic, any day can be recomputed at any
  time. This is *not* true of most telemetry and it is what makes the tool
  sufficient here.

When a live consumer arrives it should compute at **session start**, not session
end — that is when the value is needed, it is where `risk/rehydrate.py` already
reads the log back, and it avoids the teardown path entirely.

### 3.3 (Q3) NO new audit category — and this is the decision most worth arguing with

The obvious shape is a `marketdata.realized_vol` Tier 2 record written once per
session. **Recommend against, for now.**

- **The source is already durable and the function is deterministic.** A written
  RV is a cache of something reconstructible, and per 2.4 the reconstruction is
  indexed and cheap (one day ≈ 4,500 rows).
- **It cannot be written by a tool without stopping the service** (2.5). So
  "record it daily" forces either a daily stop or the in-process shape 3.2 just
  deferred. The record is what drags the whole feature into the trading process.
- **The regime precedent is the opposite of standalone recording.**
  `compute_regime` is pure and its output rides on `signal.none` /
  `signal.candidate` — the records of the decisions it informed. RV should follow
  that: when a consumer exists, the RV it *acted on* rides on that consumer's
  record, which is strictly more useful than a daily figure nobody used.

**The trigger that would change this answer**, stated now so it is recognizable
later: if the trailing window grows long enough that recomputing it at every
session start is a real startup cost. A 20-session window is ~90k rows read once,
off the loop — not a real cost. Reconsider at a window of months, or if a
consumer needs RV inside the loop rather than at startup.

*This is the decision to overrule if you want the durable series anyway — it is
one flag's worth of work either way, and the analysis above is the whole cost
side.*

### 3.4 (Q4) The window is a MODULE CONSTANT, not `signal.entry_earliest`

RV must be measured over a fixed window or the series is not comparable across
days. The natural window is **10:00 ET → last tick of the session**, matching the
2026-09-08 measurement and the decision point the entry window opens at.

**It must not be read from `signal.entry_earliest`.** That key is config and can
move; if it does, the window silently redefines itself and every historical
comparison breaks with nothing saying so. A module constant with the value
recorded alongside each computed figure keeps the series self-describing. Same
reasoning as `status.BLACKOUT_WARN_DAYS`, which is deliberately not a config key.

Report a **full-session** figure (first tick → last tick) alongside it, since the
two answer different questions and both are free.

### 3.5 (Q5) The grid is 60s, and the signature stays visible

Primary grid **60s**, a module constant. It was chosen by diagnostic, not
assumed: the measured signature is flat from 5s to 900s, so the choice moves
nothing today — but that flatness is a property of SPX being a *computed index*
(no bid-ask bounce), and it should be re-checkable rather than assumed forever.
The tool prints the signature across grids so a future regime change in the feed
is visible rather than silently biasing the series.

### 3.6 (Q6) Quadratic variation, not Parkinson

Parkinson's high/low estimator is more efficient per observation and is the usual
choice when only OHLC is available. **We have the full path**, so close-to-close
quadratic variation over a 60s grid uses strictly more information. Parkinson is
worth keeping in mind only as a cross-check if the path ever becomes sparse.

### 3.7 (Q7) A short or gappy day reports NO NUMBER, with the reason

`tools.regime`'s existing rule applies: *"no reading, and here is how long was
left when that happened"* is itself the diagnostic. A session whose path does not
span the window (2026-08-17 in the measurement; 2026-09-03's 11:04 halt) must
report absence and coverage, never a number computed over whatever it happened to
have. Carry a **coverage** figure — grid points present over grid points expected
— and refuse below a stated floor.

### 3.8 (Q8) It lives in `signal/realized.py`, beside `regime.py`

Pure, no I/O, no clock read, fully testable against a constructed path — the same
contract `compute_regime` holds and for the same reason. The log reading lives at
the seams (the tool now, a session-start reader later), exactly as
`orchestrator` owns the recording that `regime.py` refuses to do.

**It must import `MINUTES_PER_YEAR` from `regime.py`, never redefine it.** Two
copies of an annualization basis is how the ratio silently stops meaning
anything — the same rule `zerobid` exists to enforce for the tick floor.

### 3.9 (Q9) Extend `tools.regime`; do not add a second tool

`tools.evaluability`'s precedent is explicit: *"TWO QUESTIONS, ONE RUN, because
they have the same answer shape."* Here it is stronger — same records, same date
window, same `--config` plumbing, and **the interesting output is the ratio**,
which a separate tool could not produce without duplicating half of each.

---

## 4. The change, file by file

| File | Change |
|---|---|
| `src/argus/signal/realized.py` | **NEW.** Pure: a price path → annualized RV over a grid, plus coverage. Imports `MINUTES_PER_YEAR` from `regime.py`. |
| `src/argus/tools/regime.py` | Path reader (`underlying_price` by date range), an RV section per session, the IV/RV ratio on the daily series, and the signature plot. |
| `tests/test_realized.py` | **NEW.** Estimator by recovery of a known vol, per `test_regime.py`'s rule — never golden numbers. |
| `tests/test_regime_tool.py` | Reader and section coverage. |

**Not touched:** `orchestrator.py`, `strategy.py`, `session.py`, `config.py`, any
`argus.*.toml`, `docker-compose.yml`, `argus.service`. **No file the running
container can reach changes**, which by the v0.3.20 classifier makes this a
no-restart release.

---

## 5. What this deliberately does NOT do

- **It does not gate anything.** No `PreTradeGate` input, no `strategy.evaluate`
  input, no size effect. Telemetry-and-analysis only, on the `[regime]` pattern.
- **It does not add a config key.** Four of five releases around v0.3.14 forced a
  coupled config move; this one must not.
- **It does not answer whether IV−RV predicts anything.** That is a
  pre-registered test on more data (§7), and this is the instrument, not the
  finding.
- **It does not compute RV live during a session.** Same-day RV is unknowable at
  10:00 — that is the entire point of the implied number.
- **It does not touch the exit path, the watchdog, or any rail.**

---

## 6. Tests

- **Recovery, not golden numbers.** Feed a synthetic geometric-Brownian path at a
  known vol; assert recovery within tolerance. `test_regime.py`'s stated rule: a
  golden number pins whatever the code did the day it was written, sign errors
  included.
- **The annualization basis is shared, not copied.** An AST test that
  `realized.py` imports `MINUTES_PER_YEAR` and defines no second one.
- **Grid invariance.** A clean path gives the same RV across grids within
  tolerance — the property the signature plot relies on.
- **Coverage and refusal.** A truncated path reports absence plus its coverage,
  never a number.
- **Backfill agreement.** Recomputing 2026-08-05 .. 2026-09-03 reproduces the
  measurement doc's per-session RV column. This is the test that the production
  path and the ad-hoc analysis script agree.

---

## 7. Backfill, and what it unlocks

No migration and no writes: "backfill" is just running the tool over an older
date range. **27 sessions are available immediately, back to 2026-07-24** — which
means the trailing-RV clock does not start on the day this ships.

The pre-registered next test is **IV(today) vs. RV(t−1)**, the tradable form of
the IV–RV spread. It should be written down before it is run and evaluated on
materially more data than 18 sessions; §4 of the measurement doc already produced
two nulls, and continuing to slice at this n manufactures thresholds.

---

## 8. Deploy shape and risk

`src/` and `tests/` only. No config, no bind mount, no unit change — the v0.3.20
classifier reads this as **no restart**. Risk to the running system is
structurally nil: nothing in the trading path imports the new module, and the
tool is `mode=ro` with no lock. The only operational note is that the tool reads
a NAS snapshot or the live DB read-only, per the standing rule.

---

## 9. On sign-off

Nine decisions to accept, amend or overrule:

1. **The path is read back from the audit log**, not accumulated in memory
   (§3.1) — restart-safety, and backfill shares the production code path.
2. **It ships as a tool**, with the in-process version deferred until a live
   consumer exists, and placed at session *start* when it arrives (§3.2).
3. **No `marketdata.realized_vol` audit category** (§3.3). *The most arguable
   call in this document; overruling it is cheap.*
4. **The window is a module constant** (10:00 ET → last tick), never
   `signal.entry_earliest` (§3.4).
5. **60s primary grid**, with the signature plot kept visible (§3.5).
6. **Quadratic variation, not Parkinson** (§3.6).
7. **A short or gappy session reports absence plus coverage**, never a number
   (§3.7).
8. **`signal/realized.py`, pure, sharing `MINUTES_PER_YEAR`** (§3.8).
9. **Extend `tools.regime` rather than adding a second tool** (§3.9).
