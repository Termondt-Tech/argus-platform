# The flatten calibration keeps the 8:30 release days out, by rule rather than by the log (2026-09-29)

**Proposed 2026-09-29; built, not committed. Awaiting Richard's sign-off.** A tool change
only: `tools.flatten_calibration` and its tests. No runtime code, no config value, nothing
live changes. The shipped calibration (v0.5.5, 2.60 / 2.72) is reproduced exactly.

**The defect.** The tool excluded any session whose log carried `event_blackout`, on the
premise that "an event day cannot hold a position". v0.5.4 made that false for CPI, NFP and
PCE ([release days](2026-09-28-trading-the-8-30-release-days.md)). From 2026-09-30 those
days log no blackout. The next rerun would have pooled them in from that date while
keeping the five earlier ones out: a population set by what the log happened to record,
not by a decision.

**The change.** A session is excluded when the log says `event_blackout` **or** FRED lists
it as a CPI, NFP or PCE release date. On the 2026-09-28 snapshots both sources name the
same days:

| Session | Excluded by |
|---|---|
| 08-07 | `event_blackout`, NFP |
| 08-12 | `event_blackout`, CPI |
| 08-26 | `event_blackout`, PCE |
| 09-04 | `event_blackout`, NFP |
| 09-11 | `event_blackout`, CPI |
| 09-16 | `event_blackout` (FOMC) |

## Why the release days stay out, now that they are traded

The old reason is gone. The new one: **a release day is known in advance, and from 10:00 it
realizes a much lighter standardized tail than other days.** Pooling it in thins the tail the
model applies on every other day.

Same two snapshots, same tool core, k = 2 on a 5-wide:

| Sessions calibrated on | n | z₉₉ | Hill α [90%] | α shipped | Boundary |
|---|---|---|---|---|---|
| **Non-event (shipped)** | 30 | **2.60** | 3.33 [2.72, 4.74] | **2.72** | **z ≥ 3.35** |
| Pooled with the release days (FOMC out) | 35 | 2.46 | 3.65 [2.89, 5.45] | 2.89 | z ≥ 3.12 |
| The release days alone | 5 | 2.27 | 8.4 [7.1, 22.8] | 3.00 | z ≥ 2.86 |

- **The mechanism.** The uncertainty resolves at 08:30. From 10:00 these days realize less
  against about the same implied: RMS z 0.45 against 0.62, at `atm_iv` 0.168 against 0.163
  (the release-days record, found after the fact, p 0.22).
- **Pooled, the model is thin at its own boundary:** 0.54% of windows observed against
  0.50% modelled. The non-event fit is 0.45% against 0.50%.
- **On release days the non-event fit is conservative.** On the pooled 35 sessions the
  shipped boundary is passed in 0.38% of windows against 0.50% modelled. The largest
  release-day 15:30–16:00 move was 1.35σ, against 1.93σ on all days.
- **A separate release-day fit is not on offer.** Five sessions cannot fit a tail; Hill 8.4
  is noise. Revisit at ≈ 20 release days, the release-days record's binding look.

**The cost** is fewer deferrals on release days than a lighter fit would allow. It is bounded
by the whole v0.5.5 move (3.05 → 3.35, about −$0.3/day at the live fill rate), scaled to the
16% of sessions that are release days: a few cents a day.

## How the tool identifies them

- **The log's `event_blackout`**, as before. It covers FOMC, the discretionary dates, and
  every release day before 2026-09-30.
- **FRED `release/dates`** for CPI (10), NFP (50) and PCE (54) over the sessions' span,
  through `tools.blackout_dates.fetch_release_dates`. That is the blackout file's own source,
  so the two cannot disagree on a date.
- **It fails closed.** No `ARGUS_FRED_API_KEY`, a failed request, or a family with no date over
  a span longer than 45 days refuses the run. An empty answer would pool the release days in
  silently, which is the thin direction.
- **Every excluded session is printed with its sources**, so they can be read against each
  other. Before 2026-09-30 a release day carries both; from it, FRED alone.
- **The cost of the change:** the tool now needs the FRED key and the network, where it
  needed neither.

**Not chosen:**
- **The blackout file's commented record of the omitted families.** It is regenerated over a
  forward horizon, so a past release date falls out of it at the next regeneration.
- **A runtime record of "release day".** It would be a runtime change made for a tool's
  benefit.

## Also affected, not changed here

[`release_days.py`](../../RESULTS.md), the
release-days record's descriptive companion, classifies the same way: a blacked-out day
that is not FOMC or discretionary. From 2026-09-30 it will count traded release days as
"other". It needs the same source before its next rerun is read.

## Rules to carry

- **The calibration sample is non-event days.** FOMC, the discretionary dates and CPI/NFP/PCE
  release days are out, whether or not Argus trades them.
- **Putting release days back in is a decision**, with a table like the one above. Never a
  side effect of the blackout file or of what the log records.
- **An empty or failed FRED answer is never "no release days".**

Tests: `tests/test_flatten_deferral.py`, the three tests after
`test_event_days_are_read_from_the_log_itself`. They pin that a traded release day is
still excluded, the empty-family guard, and that the fetch uses the blackout generator's
fetcher and ids and refuses without a key.
