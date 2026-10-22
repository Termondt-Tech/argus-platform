# `/status`'s per-hour rate fields are startup-contaminated (2026-09-01)

**Status: FIXED 2026-09-18 (option 2, trailing-window slope), ships in
[v0.4.4](../releases/v0.4.4.md) — see [The fix](#the-fix-2026-09-18) at the end.**
Originally: DIAGNOSED, NOT FIXED. A `src/`-only rendering defect in
`status.py`, no config key, no rail. Noticed on the v0.3.14 cutover boot
(2026-09-01 17:02 EDT) and confirmed against two `/status` pulls six minutes
apart (uptime 81 s and 447 s). Recorded because `rss_growth_bytes_per_hour` is on the pre-live-flip watch
list — it is the instrument the [second v0.3.13 day](2026-09-01-second-live-fire-day.md)
finding 7 says to use, and for the first several hours of any process it reads
roughly 30x high.

## What was seen

Same process, two pulls:

| field | uptime 81 s | uptime 447 s | same values at 51.7 h |
|---|---|---|---|
| `rss_growth_bytes` | 5,345,280 | 5,345,280 | (hypothetical: RSS flat) |
| `rss_growth_bytes_per_hour` | `null` | **43,032,517** (≈ 43 MB/h) | ≈ 103,000 (0.1 MB/h) |
| `gc_collections[2]` (gen2, cumulative) | 2 | 2 | (hypothetical: no more gen2) |
| `gc_gen2_per_hour` | `null` | **16.10** | 0.039 |

RSS did not move between the two pulls — `rss_growth_bytes` is byte-identical —
and gen2 collections held at 2. **Every byte of the growth and both gen2
collections happened before the 81 s mark**, during import and first-session
warmup. Yet at 447 s the process advertises a 43 MB/h leak and 16 gen2/h.

For contrast, the real sustained figure from a 51.7 h run (finding 7) is
**1.5 MB/h** and **0.039 gen2/h**. The short-window readings overstate both by
28x and 413x.

## Why the existing guard does not catch it

`status.py:735` has `_MIN_UPTIME_FOR_RATE_S = 120.0`, and its comment is exactly
this concern — *"a 3-second-old process would report an absurd MB/hour."* It
suppresses the field below 120 s (hence the `null`s at 81 s) and publishes it
after.

That threshold cannot separate the two quantities, because **the startup cost is
a fixed amount, not a rate.** Decompose the reported figure at uptime `U`:

```
reported_rate(U) = (startup_bump + true_leak_rate · U) / U
                 = startup_bump / U   +   true_leak_rate
                   └── bias ──┘            └── signal ──┘
```

The bias term `startup_bump / U` is what the field mostly is for small `U`. With
`startup_bump ≈ 5.3 MB` it is 43 MB/h at `U = 447 s`, 2.6 MB/h at `U = 2 h`,
0.1 MB/h at `U = 51.7 h`. It decays continuously toward zero and is never
*removed* — so no fixed cutoff makes the field honest, it only makes it
less wrong. The field is trustworthy only after many hours, which is precisely
when the raw `rss_growth_bytes` is large enough to read directly and the rate
adds nothing.

`gc_gen2_per_hour` has the identical shape — `gc_collections[2]` is cumulative
from interpreter start (the `status.py:133` comment notes the division but not
the startup burst that dominates the numerator), and the import graph triggers a
gen2 sweep or two before the first `/status` is ever served.

## What it does not affect

`rss_growth_bytes` itself (an honest delta), `rss_bytes`, `gc_counts`,
`gc_collections`, `uptime_s`, and every non-derived field. The HTML view's gen2
banner (`status.py:1673`) reads the same contaminated `gc_gen2_per_hour`, so it
inherits the same overstatement for the first hours.

## Fix options, not chosen here

1. **Re-baseline after warmup.** Capture `rss_at_start_bytes` at first-session
   start, or at `uptime_s = N` minutes, rather than at process construction, so
   `rss_growth_bytes` measures post-warmup. Cheapest, and it matches what
   `rss_at_start_bytes` already claims to be — a baseline. Downside: the
   warmup-vs-leak boundary is a guess, same class of guess as `_MIN_UPTIME`.
2. **Trailing-window slope.** Keep a small ring of `(t, rss)` samples and report
   the slope over the last hour. The honest version; a leak shows up in an hour
   regardless of how long the process has been up. Costs a buffer and a session
   snapshot hook.
3. **Report only the raw delta and uptime**, drop the per-hour field, let the
   reader divide. Reintroduces exactly the "3-second process" problem
   `_MIN_UPTIME` was added for, so it would keep the suppression floor.
4. **Raise `_MIN_UPTIME_FOR_RATE_S` to hours.** Least code, still wrong — the
   bias is continuous, not a step — but it would stop the field from being
   actively misleading during the window an operator most often looks (just after
   a deploy).

Option 1 or 2. Whichever is chosen applies to `gc_gen2_per_hour` in the same
edit.

## The live-flip relevance

Finding 7 of the second-v0.3.13-day record leaves the RSS question open —
*"two retired sessions pending collection is a non-issue; a ramp that survives a
forced `gc.collect()` is not"* — and names `rss_growth_bytes_per_hour` as the
number to read. That number is reliable at the 51.7 h timescale the finding used
and unreliable at the timescale an operator reads it after a restart. Anyone
picking this up should measure from `rss_bytes` sampled hours apart, not from the
derived field, until the field is fixed.

→ [second v0.3.13 day, finding 7](2026-09-01-second-live-fire-day.md)

## The fix (2026-09-18)

**Option 2 was chosen: both rates are now a slope over the trailing hour.** Nothing
divides by uptime anywhere in `status.py`, and an AST test pins that.

- **`status.TrailingRates`** holds `(time, rss, gen2)` samples and answers the slope
  from the newest sample **at least one window old** (`RATE_WINDOW_S = 3600`) to the
  reading at poll time. It keeps exactly one anchor that old and drops everything
  older, so its history stays at about 62 samples however long the process runs.
  The span actually measured is in `[window, window + one sample interval)`, and it
  is published as `rate_window_s`.
- **A sampler task feeds it** every `RATE_SAMPLE_INTERVAL_S = 60` s
  (`StatusService.start_sampling`, started beside the heartbeat, stopped in
  `_teardown`). It is not fed by polls: a rate computed only at poll times would
  depend on who happened to look. Each tick is one `statm` read plus
  `gc.get_stats()`. **A failed sample costs that sample alone** (review fix,
  2026-09-18): the task used to die on its first exception, silently, leaving the
  rate computed against an ever-older anchor. The first failure is logged with its
  traceback, then one line per 60.
- **The window is measured on the MONOTONIC clock** (`time.monotonic`, review fix
  2026-09-18), never the wall clock: an NTP step or VM resume would otherwise stretch
  or shrink one hour's span and skew its slope. `now` still stamps the page; the
  rates take a separate, injectable `monotonic`.
- **The history is deliberately not seeded at construction.** That instant is where
  the startup cost begins, so the earliest possible anchor is the first sampler
  tick.
- **For the first hour of a process both rates are `null`.** That is the only
  honest answer there. `rss_growth_bytes` still gives the absolute figure from the
  first poll. `_MIN_UPTIME_FOR_RATE_S` is gone.

**What the fix buys, stated as the bias term.** For a trailing window, the startup
cost is inside the window only while startup is inside the hour, and after that the
bias is exactly zero. The old formula's bias decays and never reaches zero. Replayed
against this record's numbers: `null` at 447 s, where the old formula printed
43 MB/h and 16.1 gen2/h. At 2 h a flat process reads `0.0` and `0.0`, where the old
formula printed 2.6 MB/h and 1.0/h. A 1.5 MB/h leak under the same startup bump reads
1.5 MB/h (`tests/test_status_rates.py`).

**What changed for a reader.** The JSON keys `rss_growth_bytes_per_hour` and
`gc_gen2_per_hour` are unchanged, but they now mean *per hour over the trailing
`rate_window_s`*. `rate_window_s` is new. The HTML reads `… MB/h over last 60 min`,
and gen-2 prints to two decimals, so a sustained 0.039/h no longer rounds to `0.0`.
No `src/` or `tools.*` consumer read either field.

**What it does not change:** a process-start `null` means "not measured yet",
never "no growth". A trailing hour that contains the first session's warmup (the
chain subscription at the open) reports that warmup as growth. That is true growth
in that hour and leaves the window an hour later. Finding 7's question — leak, or
garbage awaiting collection — is still open. This fix makes the instrument fit to
answer it; it does not answer it.
