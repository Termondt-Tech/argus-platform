# Regime telemetry + the macro-event blackout (spec §10/§11)

*The inert skew/IV gate, the LIVE macro-event blackout, and the blackout-dates generator.*

> Relocated from `CLAUDE.md` on 2026-08-12. Settled decisions - do not
> relitigate. See [`docs/decisions/README.md`](README.md) for the index.


Two research-motivated extensions to the signal path, deliberately landed at
DIFFERENT maturities. `RegimeConfig`/`EventConfig` (`config.py`), the regime gate
and blackout check (`signal/strategy.py`), and the estimator (`signal/regime.py`)
were built together; only ONE of them changes behaviour today.

**Provenance note, because it matters for anyone reading the history.** The
`config.py`/`strategy.py` handed over with this work were described as "merged and
tested in an earlier round" and were NOT — they were a fork of a pre-chunk-1 tree
and would have reverted `ExecutionConfig`, `WatchdogConfig`, `ServerConfig`,
`MarketDataConfig`, `StalenessConfig`'s liveness floor, `AuditConfig.synchronous`,
`SignalConfig.max_width`, `StrategyResult`, and `SpreadSignal.captured_at`/
`underlying_price` — i.e. all of chunks 1–5. The delta was PORTED onto the current
files instead; the files themselves were never copied in. Anything citing that
handoff as already-merged is wrong.

- **The event blackout is LIVE.** `[events]` ships in `argus.toml` AND
  `argus.dev.toml` with `enabled = true`. On a listed date the strategy proposes
  nothing at all — `StrategyResult(None, EVENT_BLACKOUT, ...)`, before any side is
  examined. It needs no calibration because there is no threshold to fit: it is a
  categorical date list, and standing down around FOMC/CPI/NFP/PCE is practitioner
  convention for 0DTE rather than a hypothesis under test. Argus's own record
  agrees — 2026-07-29 was an FOMC decision day and the largest-magnitude loss day
  in the would-submit tally.
- **The skew/IV gate is INERT and stays that way.** `[regime]` is deliberately
  ABSENT from both configs, so `config.regime is None`, so `_side_permitted` and
  `size_multiplier` are unconditional no-ops. What runs is the TELEMETRY: a real
  `RegimeSnapshot` computed every tick and logged. **Adding a `[regime]` section is
  a live behaviour change** — do not add one to "complete" the file. It cannot be
  calibrated until the telemetry has run for weeks-to-months. External research
  ([Vilkov 2026, "0DTE Trading Rules"](../../RESULTS.md) — see
  [`docs/research/README.md`](../../RESULTS.md)) finds conditional timing
  rules outperforming an unconditional edge for comparable 0DTE structures, which
  is the closest thing in the literature to a case for eventually arming this gate
  — it is cited as an input, not a justification; no decision has been made from
  it.

Decisions that are settled — do not relitigate:

- **The estimator reads PRICES, not an IV field.** `OptionQuote` carries `delta`,
  `bid`, `ask` and nothing else — `live_chain.py`'s Greeks join maps delta only —
  so there is no per-contract IV to average. That is the right shape anyway: the
  VIX integral is defined on option PRICES (a strike-weighted sum of OTM mids), not
  on an average of per-strike IVs. **`models.py` and `live_chain.py` are untouched**,
  no new feed subscription, no Black-Scholes inversion.
- **`compute_regime` NEVER returns None and never raises.** A thin strip, a missing
  forward, or a passed expiry yields `RegimeSnapshot(nan, nan, today)`. That is
  load-bearing, not tidiness: the strategy reads the blackout DATE off this object,
  so an estimator that could return None would silently disarm the FOMC blackout on
  exactly the kind of disordered day it exists for. **The estimate may fail; the
  date may not.** A test pins it.
- **The blackout is INDEPENDENT of `regime_config`, and that asymmetry is the
  point.** `evaluate()` checks `regime is not None and regime.today in blackout_dates`
  — it never consults `regime_config`. So there is no separate "arm it" step: the
  blackout went live the moment the loop started passing a non-None regime every
  tick AND `[events]` was populated. The corollary is written at the construction
  site in `session.py`: if the regime telemetry is ever kept while `[events]` is
  dropped, `blackout_dates` must fall back to the empty frozenset, or the claim
  that the telemetry is a pure no-op stops being true.
- **The regime is logged on EVERY tick, not just `shadow.would_submit` ones.** It
  rides on `signal.none` (Tier 2) and `signal.candidate` (Tier 1), which between
  them cover every tick that has a chain. Sampling only candidate-producing ticks
  would condition the distribution on the conditions the CURRENT thresholds already
  like — precisely the wrong population to calibrate a future gate from.
  `signal.skip` (no chain) carries no regime block at all, rather than an all-null
  one that would read as "estimated, came out empty".
- **`shadow.would_submit` is deliberately UNCHANGED.** No regime block, no new key.
  It is what `shadow/outcome.py` dedups and `tools.mark` scores, and weeks of
  accumulated shadow evidence must stay comparable. Verified, not assumed: `mark`
  run against 2026-07-22 under current code and under a clean HEAD worktree
  produced **byte-for-byte identical output** (same md5), both `--dry-run` against
  SQLite-backup copies so the live DB was never opened for writing. A test also
  pins the record's key set. **NB that md5 no longer reproduces**, and the reason
  is unrelated to the regime work: the 2026-08-04 credit-column fix below widened
  `mark`'s price columns from two decimals to three. Re-run the comparison against
  a single tree, not against HEAD.
- **THE READING IS BIASED, KNOWABLY, AND `minutes_to_settlement` IS THE ANSWER.**
  The VIX integral assumes a continuum of strikes; SPX quotes a 5-point ladder, so
  discretization error grows sharply as remaining life shrinks. Measured against
  synthetic Black-Scholes strips at a constant true vol of 0.20: **1.03x at 10:00,
  1.10x at 14:00, 1.38x at 15:30, 1.68x at 15:45.** Wide markets push the same way
  (a ±1.00 bid/ask reads 1.28x where ±0.05 reads 1.03x). Two consequences for
  whoever eventually calibrates `[regime]`: (1) do **not** calibrate against
  published VIX values — this estimator and the 30-day index do not measure the same
  thing or agree in level; fit to THIS estimator's own recorded distribution.
  (2) do **not** pool raw readings across the session — condition on
  `minutes_to_settlement`, or a threshold ends up mostly detecting the clock. That
  field is recorded on every tick, including the unestimable ones, specifically so
  the bias stays correctable at analysis time instead of baked into the dataset.
- **`MIN_MINUTES_TO_SETTLEMENT = 10` bounds the ARITHMETIC, not the bias.** Below
  it the `2/T` factor turns a one-tick pricing error into an arbitrary vol. The
  measured 1.2–1.7x inflation over the last hour is well inside the guard and is
  still reported — it is smooth, monotone, and labelled, so discarding it would
  throw away the whole end-of-day sample to avoid a bias already recorded.
- **`size_multiplier` scales rail 1; it is NOT a fourth rail.** It multiplies `pct`
  INSIDE the relative bound in `orchestrator.size_contracts`, so the rail count
  stays three, tightest-still-wins, and `PreTradeGate` re-checks exactly the same
  three things. Config validation caps every multiplier at 1.0 (`<= 1` — "this
  section scales risk down, never up"), so it can only shrink. It is the one
  keyword there that DEFAULTS (to 1.0) and the asymmetry is deliberate: the caps
  have no defaults because their fail-open direction is unlimited risk, whereas
  1.0 here is "no scaling", the fail-safe direction.
- **NaN buckets as "mid", and the choice is not arbitrary.** "low" carries the
  LARGEST size multiplier, so bucketing an unreadable chain as low would hand
  maximum size to precisely the case nobody could measure. `_side_permitted`
  separately refuses nothing on a NaN skew — an unreadable chain is not evidence a
  side is dangerous, and a gate that only ever REMOVES opportunities fails safe
  toward the baseline. The rails that must fail closed on bad data (staleness, the
  pre-trade gate) are elsewhere and untouched.
- **`REGIME_SIDE_REFUSED` is ranked BELOW `NO_TRADEABLE_LEGS` in the headline
  ranking.** A side refused before its strikes were ever priced is the least
  informative rejection there is; a credit near-miss on the other side must outrank
  it, or the `signal.none` tuning dataset loses its most useful reason.

## The blackout dates file and its generator

`argus.tools.blackout_dates` is Argus's OWN script — TOMIC's FRED **pattern** was
read and reused (the `release/dates` endpoint, release ids CPI=10 / NFP=50 /
PCE=54, and the finding that FOMC has no release id), nothing was imported, and
nothing was written to TOMIC's directory.

- **`ARGUS_FRED_API_KEY`, never TOMIC's `FRED_API_KEY`** — same rule as the separate
  tastytrade app registration. It is OPERATIONAL only: `python -m argus` never reads
  it, only the generated file, so a missing key cannot affect a session and the key
  is not needed in the container. The key rides in the query string (FRED offers no
  header auth), so every error path redacts it.
- **Stdlib `urllib`, not `requests`** — same reasoning as `control.py`'s
  hand-rolled server and `notify.py`'s webhook POST: one GET against one fixed host
  does not justify a dependency in a `--require-hashes` image.
- **FOMC dates stay MANUAL, in `fomc_dates.txt`.** FRED publishes data releases; a
  rate decision is not one, so there is no release id and no authoritative API.
  Scraping federalreserve.gov was rejected for the reason TOMIC rejected it: a
  silent parsing break looks exactly like "no upcoming meetings", which would
  quietly un-blackout the most important day of the quarter. Use the SECOND
  (decision) day of each two-day meeting. `--add-fomc` appends one without hand-editing.
- **`blackout_dates.txt` is GENERATED and FRED-VERIFIED** (2026-08-04, 18 dates,
  horizon 2026-08-04 .. 2027-01-04). It was first seeded by hand from the published
  schedules; the subsequent live FRED run produced an **exactly identical date set**,
  so the transcription is confirmed rather than assumed. `--check` is clean.
- **It runs out after 2026-12-23, and that is FRED's limit, not an oversight.** The
  agencies have not published 2027 schedules yet — asking for a 12- or 18-month
  horizon returns the same 18 dates and trips the `runs out early` warning.
  `fomc_dates.txt` likewise has no 2027 entries. **Re-run the generator when the
  2027 calendars publish.** A blackout list that has ended is indistinguishable,
  from inside the process, from a day with no scheduled event.
- **`include_release_dates_with_no_data` MUST be `true`, and this bit us.** The
  first live run returned zero CPI/NFP/PCE dates. "No data" does not mean "no such
  release" — it means SCHEDULED BUT NOT YET PUBLISHED, which is precisely and
  exclusively what a forward-looking blackout is made of. With it `false` the
  endpoint returns `count: 0` for any future window. A test pins the parameter by
  name, and `_refuse_if_any_event_is_empty` now REFUSES to write a file where any
  of the four events has no dates: all four publish at least monthly, so an empty
  list over a real horizon is a broken query, never a quiet calendar. Without that
  guard the tool reported "NONE" three times and would still have written a file
  that suppressed nothing on CPI, NFP and PCE days — the exact silent-coverage
  failure this feature exists to prevent, one level up.
- **`realtime_start`/`realtime_end` bound the window server-side**, so the response
  carries only the dates asked for. CPI alone has ~950 release dates back to 1949;
  a client-side filter against a default row limit would eventually truncate from
  the FUTURE end, which is the only end that matters here.
- **A missing file is a hard `ConfigError`, not an empty blackout.** `enabled = true`
  against no file means the operator believes entries are suppressed while nothing
  is. A malformed line raises too — a silently-dropped FOMC date defeats the point.
- **DEPLOY COUPLING, and it is the same trap the `[watchdog]`/`[risk]` sections
  have.** `docker-compose.yml` now bind-mounts `./blackout_dates.txt` to
  `/etc/argus/blackout_dates.txt` beside the config. The file must move WITH
  `argus.toml`: this image against a pre-`[events]` config is fine (the section is
  optional), but this config against a missing data file refuses to start. `git pull`
  before `docker compose build`, always. Like the config, the file is versioned by
  git on the droplet and does NOT roll back with the image tag.

Tests: `test_regime.py` (estimator by RECOVERY of a known Black-Scholes vol, never
golden numbers — a golden number pins whatever the code did the day it was written,
including a sign error), `test_strategy_regime.py`, `test_config_regime_events.py`,
`test_orchestrator_regime.py`. The FRED fetch HAS now been run for real against the
live API (2026-08-04). Nothing else here has been run against sandbox; the estimator
has only ever seen synthetic strips.

## Amendment: the 8:30 releases leave the blackout (2026-09-28)

CPI, NFP and PCE are omitted from `blackout_dates.txt` by policy (`tools.blackout_dates
--omit`), so entries are taken on their days. FOMC and the discretionary dates stay. The
event blackout itself remains LIVE and needs no `[regime]` section. The record has the
mechanism, the evidence, a pre-registered abort threshold and the rollback.
→ [trading the 8:30 release days](2026-09-28-trading-the-8-30-release-days.md)
