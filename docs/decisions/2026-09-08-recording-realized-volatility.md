# Recording realized volatility

*The level `atm_iv` is a level against — built 2026-09-08, `src/` only, no config
key, no audit category, telemetry-and-analysis only.*

> Settled. Design and rejected alternatives:
> [`docs/designs/2026-09-08-recording-realized-volatility.md`](../designs/2026-09-08-recording-realized-volatility.md),
> approved 2026-09-08, all nine §9 decisions as written. See
> [`docs/decisions/README.md`](README.md) for the index.

`signal/regime.py` has estimated IMPLIED vol on every tick since 2026-08-12.
Nothing computed REALIZED vol, so `atm_iv` was a level with nothing to be a level
against — the difference between knowing how far the market will move and knowing
whether the premium is RICH. `signal/realized.py` closes the arithmetic half, and
`tools.regime` reports the two together.

**Nothing acts on the number.** No gate input, no size input, no config key, no
new audit category — the `[regime]` pattern exactly. `src/` and `tests/` only, so
by the v0.3.20 classifier this is a no-restart release.

## What was built

- **`signal/realized.py`** — pure, no I/O, no clock read, no config, mirroring
  `compute_regime`'s contract. Close-to-close quadratic variation over a fixed
  grid, annualized on `MINUTES_PER_YEAR` **imported from `regime.py`**.
- **`tools.regime`** gained the path reader, RV and IV/RV columns on the daily
  series, an IV-vs-RV aggregate, and the volatility signature plot.

## The decisions, and the ones worth carrying

**The path is read back from the AUDIT LOG, never accumulated in memory.** Argus
restarts inside sessions — deploys do, and 2026-09-03's cert-502 halt did at
11:04. An in-memory accumulator coming up at 13:00 computes an afternoon and
**labels it the day**, with nothing in the number saying so. The recorded path has
the whole session regardless of how many processes wrote it. The second reason is
that this makes backfill and live the same code path: 27 historical sessions
became a test of production code rather than of a one-off script, and **all 19
sessions shared with the 2026-09-08 measurement reproduce its RV column exactly.**

**It ships as a TOOL; the in-process version is deferred until a consumer exists,
and belongs at session START when it arrives.** Nothing reads trailing RV yet, and
session teardown is where `_end_day` once took the watchdog down with it. The
usual objection to human-scheduled telemetry — it develops gaps — does not apply
**because the raw path is durable and the computation deterministic**, so any day
is recomputable at any time. That property is what makes a tool sufficient here
and is not true of most telemetry.

**NO `marketdata.realized_vol` audit category.** A written RV is a cache of
something reconstructible, the reconstruction is indexed (migration 0003's
`(category, ts)`) and cheap, and a tool cannot write to the live DB without
stopping the service — so "record it daily" drags the whole feature into the
trading process. The regime precedent is the opposite of standalone recording:
`compute_regime` is pure and its output rides on the record of the decision it
informed. RV follows that. **The trigger that would reverse this**, stated so it
is recognizable later: a trailing window long enough that recomputing it at
session start is a real cost. A 20-session window is ~90k rows read once, off the
loop. Reconsider at months, or if a consumer needs RV inside the loop.

**`MINUTES_PER_YEAR` is IMPORTED, never redefined**, held by an AST test. The one
number anybody wants out of this module is IV/RV, and two copies of an
annualization basis is how a ratio silently stops meaning anything — the rule
`execution/zerobid.py` exists to enforce for the tick floor.

**`WINDOW_START_ET` is a module constant and must never read
`signal.entry_earliest`**, even though the two hold the same value today. That key
is config and can move; if it did, the window would redefine itself and every
historical comparison would break with nothing saying so. Held by an AST test
asserting the module imports nothing from `argus.config` — and asserted over the
AST rather than the source text, so the comment explaining the rule does not trip
the test enforcing it. Same call, same reasoning, as `status.BLACKOUT_WARN_DAYS`:
a REQUIRED key would force a coupled deploy for a number nothing trades on.

**`GRID_SECONDS = 60` was chosen by DIAGNOSTIC, and the diagnostic stays
printed.** The measured volatility signature is flat from 5s to 900s, because SPX
is a computed index over 500 constituents rather than a traded price — no bid-ask
bounce, and the mild dip at 5s is constituent staleness. So the choice moves
nothing today. The tool prints the signature on every run anyway, because that
flatness is a property of the FEED and a change to it should be visible rather
than silently biasing the series.

**Quadratic variation, not Parkinson.** Parkinson's high/low estimator is more
efficient per observation and is the usual choice when only OHLC is available; we
have the whole path, so QV uses strictly more information. Revisit only if the
recorded path becomes sparse.

## The refusal taxonomy, and what it immediately caught

A short or gappy session reports **no number and the reason** — `tools.regime`'s
existing "no reading, and here is why" contract. Two refusals, because they are
different problems and **one metric would not have separated them**:

- `span_too_short` — the path does not cover the window. TRUNCATION.
  `MIN_SPAN_FRACTION` is a fraction of the requested window, not an absolute
  number of minutes, so **a half-day is a short session and not a broken one**; a
  literal minimum would refuse every half-day.
- `coverage_too_low` — holes WITHIN the sampled span. Tier 2 telemetry is
  droppable (`AuditLog.telemetry` returns False on a full queue), so the recorded
  path is expected to be sparser than the cadence; quadratic variation tolerates
  that, and scattered drops of 50% still recover the vol. What it cannot tolerate
  is a hole.

**It caught a real defect in the measurement that motivated it, on its first
run.** 2026-09-03 has a span of 359 of 360 minutes and **coverage of 0.24**, its
largest hole being **273.9 minutes** — the 11:04:03 halt to the 15:37:57 manual
restart. A span test alone passes that session. It was the 2026-09-08 study's
**only counterexample** — the sole session with IV < RV — and its RV was computed
over the recorded 24%, which over-weights the volatile pre-halt morning and the
last 22 minutes. On the gated sample the study's headline moves from **IV/RV 1.182
(t = 5.27), IV > RV on 16 of 17** to **1.215 (t = 9.33), 15 of 15**, and
`corr(atm_iv, RV)` from **+0.842 to +0.964**. Every conclusion strengthens, and
the one that had to be re-checked rather than assumed — the premium being FLAT
across IV levels, which is what removed the mispricing rationale for
`size_multiplier_*` — survives: `corr(atm_iv, IV/RV)` moves to −0.285 with a 95%
bootstrap CI of **[−0.69, +0.12]**, spanning zero, terciles 1.29 / 1.19 / 1.19.
→ [the measurement, corrected](../../RESULTS.md)

## Two smaller rules worth carrying

**Annualization is over the SAMPLED SPAN, never the requested window.** Quadratic
variation covers the period actually observed; dividing by a window the path did
not span would understate every truncated day instead of refusing it. The refusal
keeps the series comparable, and this keeps the arithmetic honest for the days
that pass it.

**The price path is keyed off the TIMESTAMP, the opposite of `read_readings`.**
Regime readings are keyed off the payload's own `today` because the process
stamped it and the blackout check consulted it. `underlying_price` predates the
regime block, so a payload date is unavailable for most of the backfill — and
unlike a regime reading, a tick's ET date is unambiguous on its own, since a
session crosses no midnight. Keying the path on `today` would silently drop every
pre-2026-08-12 session, which is most of the history this reads.

## What this deliberately does NOT do

No gate input, no size effect, no config key, no live computation during a session
(same-day RV is unknowable at 10:00 — that is the entire point of the implied
number), and no answer to whether IV−RV predicts anything. That is a
**pre-registered** test on materially more data than 18 sessions; the 2026-09-08
pass already produced two nulls, and continuing to slice at this n manufactures
thresholds. Usefully, the clock does not start now: the path runs back to
**2026-07-24 — 30 sessions, 27 spanning 10:00→close** — so trailing RV is
backfillable today.

Tests: `tests/test_realized.py` (recovery of a known vol, never golden numbers —
`test_regime.py`'s rule; grid invariance; the two refusals separated; the shared
basis and the config-free property held by AST), plus the reader and half-day
window in `tests/test_regime_tool.py`.
