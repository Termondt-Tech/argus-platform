# Trading the 8:30 release days: CPI, NFP and PCE leave the blackout (2026-09-28)

**Decided 2026-09-28 (Richard).** **Deployed in v0.5.4, 2026-09-28** (`/status` read back config sha `d6639d0d7a1a`; first release day traded: 2026-09-30). It is a data change plus a tool change:
- `blackout_dates.txt` is regenerated with `tools.blackout_dates --omit CPI --omit NFP --omit PCE`.
- The generator gains `--omit`.

No runtime code or config key changes, and the runtime reads the file exactly as before.
FOMC days and the discretionary dates stay blacked out.

**What moves:** 10 of the 63 sessions from 2026-09-28 to 12-24 are now traded.

| Family | Days now traded |
|---|---|
| PCE | 09-30, 10-29, 11-25, 12-23 |
| NFP | 10-02, 11-06, 12-04 |
| CPI | 10-14, 11-10, 12-10 |

Still blacked out: FOMC (10-28, 12-09) and the discretionary dates (10-15, 10-20, 11-27,
12-24). An omitted family's dates stay in the file as comments, a record the loader never
reads. The file's header names the flags that reproduce it.

## Why

**The mechanism.** CPI, NFP and PCE print at 08:30 ET. That is 70 minutes before
`market.session_open` and 90 before `entry_earliest`, so by the first eligible tick the 0DTE
chain has repriced to the post-release state. The event premium the blackout avoids is
already resolved.

**The evidence behind the blackout never concerned these days.** Its record is practitioner
convention plus one day: 2026-07-29, an FOMC day with a 14:00 intraday event
([regime decision](regime-telemetry-and-event-blackout.md)). That mechanism is different,
and FOMC stays out.

**What the record says about release days.** This is descriptive and found after the fact:
[`release_days.py`](../../RESULTS.md),
[due diligence](../../RESULTS.md) §3. The measure is
realized against 10:00–15:00 implied, as RMS z of the move to the close; below 1 is good for
a seller.

| Days | n | RMS z, mean (median) | `atm_iv` at 10:00 |
|---|---|---|---|
| 8:30 release | 5 | **0.45** (0.34) | 0.168 |
| every other day | 27 | 0.62 (0.52) | 0.163 |

The release days realized *less* than they implied, at the same implied level. The
difference is −0.17, with a permutation p of 0.22: not significant, and not the direction
the blackout assumes.

**What standing down costs.** 16% of sessions. If a release day carries the book's average
day, that is ≈ +$2–4/day averaged over all sessions, at a modelled book mean of +$13–23.

**The risk is bounded by the rails that bound every day.**
- The strike-touch stop, one open spread per side, 2 × 475, and the 1050 loss budget, which
  bounds the day on every path.
- A post-release trend day is the case that matters. It costs the book's usual bad day, two
  touch stops ≈ −$370, not a new kind of loss.

## The forward test (pre-registered, fixed before the first release day)

**Population.** Every CPI, NFP or PCE release day traded from 2026-09-30 on, using the dates
the regenerated file records as comments. A release date that coincides with FOMC or a
discretionary date is still blacked out and is not in the population.

**The one policy parameter: the abort threshold, −$600.**
- If cumulative net P&L on release days reaches −$600 or worse at any close, the three
  families go back in at the next deploy.
- Net P&L is the ledger's, broker fees included; per-day `tools.ledger` output is the
  source.
- Going back in means regenerating without `--omit`: a data-only release, and a restart.
- **Why −$600:**
  - At the modelled day distribution (SD ≈ $100) that is about 2.5 SD below expectation
    after 10 release days.
  - Alternatively, it is two worst-case days (two touch stops each) plus change.
  - Loose enough not to fire on one ordinary bad day, tight enough to stop a real problem
    inside a quarter.
  - Changing it is Richard's call and goes in this record.

**The binding look: at 20 release days** (≈ April 2027).
- Compare day-level net P&L on release days with non-release days over the same span, with a
  one-sided permutation test for harm at α = 0.10.
- Release days worse at p < 0.10 → they go back in.
- Otherwise the omission stands, and this record is amended to say so.
- The low α bar is deliberate: the burden is on showing harm, and 20 days cannot show a
  benefit of this size.

**A descriptive companion, not a gate.** Rerun `release_days.py` on each new snapshot, so the
realized-to-implied comparison is updated with live release days.

## Rollback

- Regenerate with `python -m argus.tools.blackout_dates --out blackout_dates.txt` (no
  `--omit`).
- Deploy as a data-only release. The tool fetches and validates the omitted families on
  every run, so their dates are always current.
- `tests/test_config_regime_events.py::test_the_shipped_blackout_file_loads` pins the
  omission both ways. Rolling back means updating that test deliberately, which is the point.

## Rules to carry

- **FOMC can never be omitted.** The tool refuses it: it is an intraday event, the one the
  blackout's evidence is about.
- **Omission is policy, never a fix for a broken query.** An omitted family is still
  fetched and still held to `_refuse_if_any_event_is_empty`.
- **Regenerate with the flags the file's header names.** A regeneration without them puts
  the families back (the safe direction), and the shipped-file test fails. The failure is
  the reminder.
- **Discretionary dates are unaffected.** A date shared with an omitted family stays blacked
  out.
