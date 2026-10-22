You are auditing Argus (this repo) against its OBJECTIVE. Work read-only on the code;
your only write is one review document, on a new branch, opened as a PR.

OBJECTIVE (being added to CLAUDE.md in PR #24; treat it as authoritative):
Argus exists to make money for its investors: consistent, long-term profit from selling
0DTE SPX premium, net of fees and slippage. The non-negotiables, rails and audit log are
CONSTRAINTS on how that is pursued, never the objective. The measure is net P&L per
trading day (the day is the unit of evidence), judged against day SD, the worst day and
drawdown. The mean-vs-tail exchange rate is the owner's (Richard's) call. The standing
question for any change: what does it do to net expected P&L, and to that risk?

THE QUESTION: where does the current implementation (as on main: v0.5.3, live config
argus.live.toml) leave net expected P&L on the table, or pay for risk reduction without
stating the price? Find, rank and quantify. Do NOT relitigate the non-negotiables in
CLAUDE.md (kill switch, one audit writer, fail-closed audit, broker as truth, single
multi-leg orders, no broker-side stops, sandbox before live, the welded size invariant,
the credit_multiple prohibition's evidence standard). A finding may say what a rail
costs; it may never recommend breaking one.

READ FIRST: CLAUDE.md; docs/0dte_spx_desk_spec.md; docs/decisions/README.md, then the
decisions for each component you assess; docs/research/README.md and the notes it
indexes, especially 2026-09-21-post-stop-reentry-and-stale-greeks.md,
2026-09-25-two-slot-side-composition.md, 2026-09-26-cross-document-exploration.md and
2026-09-28-intraday-vrp-term-structure.md; docs/releases/v0.5.2.md and v0.5.3.md;
argus.live.toml, including its comments.

AUDIT SURFACE (a starting list; follow the money wherever it leads):
1. Entry selection: 0.15-delta short on mid-implied delta, min_credit 0.50, width 5 and
   max_width, the ladder-gap long leg, side order and the side-balance rule, candidate
   dedup, the entry window (entry_earliest 10:00: the 10:00 macro-release jolt;
   entry_latest 15:00 against the 15:30 flatten), and the event and discretionary blackouts.
2. Entry pricing: the walk (execution.walk_source values are still the 2026-08-10 SANDBOX
   set), rung concessions, walk-away, COA waits. What is paid relative to mid, and is it
   measured on live fills?
3. Exits: the strike-touch stop (package B), the 15:30 flatten and its far-OTM deferral,
   the exit walk (exit_walk_min_steps 3, exit_walk_away_pct), the width-capped cross, the
   profit target (built, OFF). Where does an exit sell premium back that holding would
   keep, and where is that cost stated?
4. Capacity and sizing: 2 x 475 against a 1050 day, the loss-budget cliff (a slot the
   budget refuses), the side rule's effect on slot use, max_contracts 1 against
   per-contract fees, position_size_pct. How much of the day is capital idle, and why?
5. Friction: per-contract fees (the research replays use $3.30 to open, $1.30 to close),
   the quoted half-spread, crossing, closing versus expiring (expiry pays no close fee).
   Friction as a share of gross edge per trade.
6. Availability: every path that refuses or ends entries (staleness gates, the greeks
   bound, guard-unreachable streaks, halts, blind or degraded states, restarts), and what
   is known about how often each fires in live and what it costs.
7. Measurement gaps: P&L the system cannot attribute (realized fill versus mid per leg,
   chain strikes around the short that are not recorded, idle-capacity time, per-exit
   cost versus holding). A decision nobody can measure cannot be optimized.
8. Uncalibrated parameters: every live value chosen by hand or carried from sandbox
   rather than fitted to live data. Name it, its current value, its provenance, and what
   would calibrate it.

METHOD AND EVIDENCE STANDARD:
- Cite file:line for every claim about behaviour; check claims against the code and
  config, never against prose alone (reviews here have repeatedly found stale prose).
- Quantify in $/day or return on width wherever the committed record or code allows,
  and give the source of every number. Where a number needs the audit log, say exactly
  which categories and fields, and the query. The NAS snapshots (dev/nas/, gitignored)
  are NOT available in this environment: never guess what they would show, and never
  try to reach the droplet, a live database or the broker.
- Mark every number as MEASURED (with its source), MODELLED (with the model) or
  UNKNOWN (with the measurement that would settle it).
- Research-note numbers are exploratory unless a decision record cites them; say which.
- Report every candidate you evaluated, including the ones you dismissed, and why.

DELIVERABLE: docs/reviews/2026-09-28-objective-profit-audit.md, following the
conventions of the existing docs/reviews/ files and docs/research/README.md's
"Adding a review" rules (tag every finding exactly once; root-cause the mechanism;
recommend only what the evidence supports). Structure:
  1. Summary: the three to five largest leaks, with estimated $/day and confidence.
  2. Findings table: id, area, mechanism, estimated impact (and whether MEASURED,
     MODELLED or UNKNOWN), evidence (file:line), the rail or decision it touches,
     confidence, status tag.
  3. One section per finding: mechanism, evidence, impact, what would settle it (a
     query or a pre-registered test in the style of the forward pre-registrations in
     docs/research/), and the recommended next step (a decision record to write, a
     measurement to add, or nothing).
  4. The measurement plan: every query that needs the live audit log, written out so
     Richard can run it read-only locally
     (sqlite3 "file:dev/nas/<snapshot>.db?mode=ro").
  5. What this review does NOT license: no config or src change follows from it
     directly; each change needs its own decision record.

CONSTRAINTS: no changes to src/, tests/, any argus.*.toml, scripts/ or deploy files.
Create one branch, add only the review file (and a row for it in docs/reviews' index,
if one exists), open a PR against main, and end the PR description with the repo's
usual attribution line. Keep prose tight: the owner has a prop-trading and exchange
background, so assume market-structure fluency and lead with findings, not narrative.
