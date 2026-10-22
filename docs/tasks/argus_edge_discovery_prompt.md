# Edge discovery: find a trading idea that makes money after costs, at a size worth scaling

Paste everything below the line into a new session, started from the repo root.

---

## Why this exists

**Richard's goal is to make trading his full-time career.** That needs an edge that is
real, survives every cost, and still works at many times today's size. The 2026-09-30
study showed today's 0DTE credit-spread book has none. This work searches for one, fast,
and without fooling itself.

## Before beginning

1. **Read `CLAUDE.md` in full.** Its Objective governs this task: Argus exists to make
   money for its investors, and net P&L per trading day, judged against its risk, is the
   measure. Its non-negotiables and standing rules apply to anything this work might one
   day put live.
2. **Read these, in order:**
   - `docs/research/2026-09-30-thetadata-history-results.md`: why today's book has no
     edge. This is your starting evidence.
   - `docs/research/2026-09-30-history-queries/PREREGISTRATION.md`, including all six
     addenda: the evidence standard this work is held to.
   - `docs/research/2026-09-30-history-queries/README.md`: the tooling to reuse
     (`thetadata_common.py`, `derive.py`, `book.py`, `score.py`).
   - `docs/research/README.md` and the notes it indexes. Skim them all; read the
     2026-09-15 external research review in full.
   - `docs/research/2026-09-29-historical-options-data-survey.md`: data sources and
     prices.
3. **Standing rules, restated because they apply to every session:**
   - **Never write to `argus-live.db` or `argus.db`.** Every read of either is read-only
     (`sqlite3.connect("file:...?mode=ro", uri=True)`). Every dev or tool run uses
     `argus.dev.toml`.
   - **Work only in `docs/research/<date>-edge-discovery/` and under `dev/`.** Do not edit
     `src/`, any `argus.*.toml`, or anything else without asking; Richard runs parallel
     sessions.
   - **Never edit `docs/research/2026-09-30-history-queries/`.** That study is frozen.
     Import from it.
   - **Never commit or push without asking.**
   - **Vendor data is personal-use.** Raw and derived vendor data live under `dev/` only
     and are never committed.
   - **Vendor code runs in `dev/history/.venv`, never Argus's venv.** Always build the
     ThetaData client through `thetadata_common.connect()`; the library otherwise loads
     Argus's `.env` (trading credentials) into its process. The API key is in the macOS
     Keychain, service `thetadata-api-key`.
   - **polars orders NaN above every number.** Test quotes with `thetadata_common.offered()`.
   - **Request no SPX or SPXW session on or after 2026-09-28** unless Richard releases
     that date range. Live forward tests (F1, F2, V1 and the release-day test) run on
     those days.

## The task

**Find a trading idea that has a real edge, and that makes a good, scalable profit after
every cost.**

- **Start greenfield.** Do not tune today's 0DTE credit-spread book: the history closed
  it. Do not assume the answer is selling premium, or 0DTE, or even SPX.
- **Move quickly by working in parallel and killing ideas early,** not by lowering the
  evidence standard.

**What AI changes here, and what it does not.** Your advantage is breadth, speed and
discipline. You can test fifty mechanism-grounded ideas in the time a person tests three,
and log every one honestly. It is **not** open-ended pattern-mining of price history. A
long history shows a "winner" for almost any search by luck. Every idea needs a
mechanism: **who is on the other side, why they pay, and why that persists.**

**"Nothing found" is an acceptable answer.** Report it plainly if it is the truth. A false
positive costs real money.

## What we already know (use it; do not re-derive it)

- **The 0DTE premium at ~0.15 delta, with a strike-touch stop, is priced about fairly.**
  One trade in five is stopped, and a stop costs about four winners. Before fees the book
  made about $6.65 a session.
- **Friction decided it.** At the fill quality Argus's live fills show (h = 1/3 of the
  half-spread given up), costs ran about $9 to $13 a session for about two 5-wide round
  trips a day. Fees are $3.44 to open and $1.44 to close a contract.
- **Every variant that did less badly cut friction:** closing early less, one wide spread
  instead of two narrow ones, selling farther from the money, trading fewer days. None
  crossed zero after costs.
- **Afternoon index premium is not richer than midmorning's** (2020–2026, every year).
- **The 8:30 release days (CPI, NFP, PCE) cost a short-premium seller money** in
  2022–2025, and were harmless in 2020–2021.
- **10:00 ET carries a jolt:** the 10:00–10:03 move runs +0.59 implied sd above quiet
  minutes (minute grid, 2020–2025).
- **The history's tails are thinner** than the live calibration (Hill α about 4.5 against
  2.7).
- **Model marks are slightly pessimistic against real mids** (+$0.01 to +$0.03).

**The data in hand, pulled 2026-09-30:**

- **What it covers:** SPXW 0DTE, **every listed strike** of each session's same-day
  expiration, both rights, the last NBBO at each minute from 09:30 to 16:00.
- **Columns:** `bid`, `ask`, **`bid_size`, `ask_size`**, exchanges and conditions.
- **Sessions:** 1,435, from 2020-01-02 to 2026-07-17 plus 2026-09-15 to 09-25.
  Mondays, Wednesdays and Fridays only before 2022-06.
- **Where it lives:** `dev/history/thetadata/spxw-1m/{history,heldout,validation}/`, about
  0.9 GB. Loading 2025-10-01 to 2026-07-17 needs `thetadata_common.load_day(...,
  allow_held_out=True)`. The old study's one look at that period is spent; for new
  families it sits inside the new locked set (below).
- **Also in hand:**
  - `dev/history/thetadata/spx-eod.json`: the official close of every session, from
    FRED.
  - `docs/research/2026-09-30-history-queries/calendars.json`: FOMC, CPI, NFP and PCE
    days.
  - `dev/history/derived/`: ±150-point windows built for the old book only. Build your
    own.
- **Not in hand:** other expirations, other underlyings, the overnight price of anything.
  **The subscription probably reaches them** (ThetaData Options Value, 1-minute from
  2020, 2 concurrent requests). Verify coverage in the vendor's own docs.

**The live infrastructure is SPX multi-leg via tastytrade:** OAuth, reconciliation, the
watchdog, the audit chain. **An idea on SPXW is the shortest path to live**, which is why
Round 1 below uses the data in hand.

## Checkpoint 0: before any data is read

One short exchange with Richard. Present each item below as a proposal and get his yes or
his edit. Write the answers into `docs/research/<date>-edge-discovery/PLAN.md` and ask
him to commit it before the first screen. **That commit is the program's
pre-registration.**

These values were advised on 2026-09-30 and Richard asked for them to be proposed
exactly. Present them as written; change one only if he edits it.

1. **The locked years.** Develop on **2020-01-02 to 2023-12-29** (about 751 scored
   sessions); lock **2024-01-02 to 2026-07-17** (about 611).
   - **Each side holds real stress.** Development has the 2020 crash and the 2022 bear
     market; the locked years have 2024-08-05 and 2025-04-07 to 10.
   - **The locked years are the most recent,** so confirmation runs on the market closest
     to live. Calendar boundaries keep Stage 1's year rule simple.
   - **Contamination, stated:** the index path over the locked years, and the old book's
     results there, have been seen. No P of any new family has been.
   - **The engine enforces the lock in code.** A locked date is refused unless a committed
     pre-registration names the finalist being confirmed.
   - The same split applies to every data source, including new pulls.
2. **The success bar ("good profit we can scale").**
   - **Statistical:** on the locked years, the after-cost session u(P) (R = $19,700, the
     one policy parameter, §4 of the 2026-09-30 pre-registration) is positive by a
     one-sided sign-flip permutation test at α = 0.05 ÷ the finalist cap.
   - **Economic:** at the size Argus's rails would allow (a daily loss budget of 3% of the
     account, the structural worst case of the day's positions), at least 15% a year after
     costs. **Equivalently, and independent of account size: the mean session P is at
     least 2% of the day's loss budget per unit** (252 × 0.02 × 3% ≈ 15%).
   - **Drawdown:** at that size, the largest drawdown is at most 20% of the account.
   - **Consistent:** positive after costs in at least two thirds of calendar years.
   - **Scalable:** at 25× today's size (about a $1M account), at least 90% of fills fit
     inside the displayed `bid_size` or `ask_size` at the NBBO. The rest are priced at the
     natural price plus one tick, and the after-cost mean must stay positive.
   - **Cost-robust:** break-even h ≥ 0.5, meaning it still breaks even when fills give up
     half the quoted half-spread.
   - **What the statistical bar can see.** On 611 locked sessions at α = 0.05/3, a
     strategy needs an after-cost annualized Sharpe of about **1.9** to pass four times
     in five, and one at Sharpe 1.0 passes about 28% of the time. A real but moderate edge
     can fail the locked check. Its route is a pre-registered live forward test at
     minimum size, never a looser bar here. Report each finalist's power beside its
     result.
3. **The budgets.**
   - **Money:** keep the ThetaData Options Value subscription (about $40 a month).
     Every other purchase needs Richard's approval first.
   - **Disk:** at most 50 GB. About 629 GB is free; Round 1 needs about 1 GB more.
   - **The search:** at most 20 ideas reach Stage 1 in each round, with at most 5 variants
     each.
   - **Time:** Round 1 reports within about five working sessions, one to two weeks.
   - **Agents:** at most 7 scouting agents at once, and one red-team agent per finalist.
     Parallel agents use Richard's plan quickly; this keeps it predictable.
4. **The finalist cap:** at most **3** finalists, ever, on the locked years. **Each is
   tested at one-sided α = 0.05 / 3 ≈ 0.017 whenever it arrives** (a Bonferroni split,
   because finalists can arrive in different rounds and Holm needs them all at once).
   - The Sharpe needed to pass four times in five, by cap: 1 finalist 1.60, 2 finalists
     1.80, 3 finalists 1.91, 5 finalists 2.03.
   - When three have been tested, the locked years are spent.

## Phase 0: the engine and the state file (build once, trust once)

**`STATE.md`** in the working directory holds what is done, what is decided, the
scoreboard's top lines and the next three steps. Update it at the end of every stage and
before any long run. Keep a short memory handoff note pointing at it. **Every new session
starts by reading it.** This is weeks of work, not one context window.

**The engine** is one shared codebase, used by every agent:

- **Data:** loads a session's quotes as bid, ask and size matrices by (right, strike,
  minute). **It enforces the lock.**
- **Simulator:** any defined-risk structure of one to four legs, entered as one multi-leg
  order.
  - Entries and exits at a minute mark, with stops and time exits as rules.
  - Fills give up h of the half-spread, for any h; fees by product.
  - Settlement on the official close.
  - Spot is the parity forward over every listed strike: import `regime.implied_forward`
    and the fifth addendum's rule, and never copy them.
- **Statistics:**
  - day-level P and u(P); by year; the worst day; drawdown; break-even h; capacity
    against quoted size;
  - **the ledger:** every run is appended automatically by the engine, not by the
    analyst;
  - the max-statistic correction computed over the whole ledger.
- **Tests** for the arithmetic: settlement, fills, fees and parity, on hand-built cases.
- **The known-answer gate.** On the development years only, the engine reproduces
  `book.py`'s incumbent P session by session: to the cent, or with every difference
  explained. **Nothing is screened until it passes.**

## Round 1: the data already in hand

Zero pull time, tooling to reuse, and the shortest path to live.

1. **The map** (development years only; it measures ceilings, not strategies).
   - For both rights, across moneyness and minute of the day: the premium sold against the
     payout realized to the close, next to the quoted cost of trading it.
   - The long side's ceiling as well as the short side's.
   - Release days against other days, the 10:00 jolt, the last hour, Mondays against
     Fridays, opex, month-end.
   - One pass answers the ceiling question for dozens of ideas at once.
2. **One branch-deciding pull: is index premium earned overnight?**
   - For each session, pull the next session's expiration from 15:30 to 16:00. Its
     morning is already in hand as that day's 0DTE.
   - Pull one day first to measure its size and time; expect under 1 GB in all.
   - Analyze the development years; the locked years are pulled but stay locked.
   - Literature to verify first-hand: Muravyev & Ni, "Why do option returns change sign
     from day to night?" (J. Financial Economics, 2020). If the premium is earned
     overnight, a pure intraday seller sits on the wrong side of the clock.
3. **Scouting, in parallel** (Richard asks for subagents here).
   - Each agent covers one area and splits its ideas in two: **testable on the data in
     hand** (Round 1), and **needing new data** (the Round 2 inventory, not tested yet).
   - For each idea: the mechanism; who pays and why it persists; the expected size
     against its round-trip cost; the data needed; the build distance from Argus; the
     capacity; the tail risk; and sources.
   - **Every source is read first-hand and cited with a link.** Nothing cited from memory.

   The areas:

   1. When the premium is earned: time of day, overnight, day of week, days to expiry.
   2. Risk premia beyond at-the-money variance: skew and the crash premium, the term
      structure, event premia (do buyers or sellers win on release days?).
   3. Predictable flows:
      - retail 0DTE automation on round clock minutes (the Cboe Q2 2026 note);
      - closing imbalances, and pinning into the close;
      - leveraged and VIX ETP rebalancing;
      - month-end and opex effects;
      - dealer gamma (read the Amaya et al. paper in the repo first).
   4. Execution as the edge: resting orders at or inside the mid, the complex-order
      auction, a lower cost per unit of premium. Minute quotes cannot show whether a
      resting order would fill; say so wherever it matters.
   5. Conditional entry: state known at entry. The four skip filters found little. Read
      the 2026-09-15 review of Vilkov (2026) before using its conditional rules, which are
      47.8% long.
   6. Other structures and instruments: butterflies, condors, broken wings, ratio spreads
      in defined risk; SPY, QQQ, NDX, RUT, VIX, /ES options.
   7. A wildcard: who is forced to trade, and at what price?

   **Seed examples for Round 1** show the kind of idea meant, not a list to work through:
   - buying premium on release days, where sellers lost;
   - trading around the 10:00 jolt;
   - last-hour structures;
   - butterflies and condors (one order, a different cost per unit of premium);
   - holding to expiry, farther from the money.
4. **The funnel** (below) on the Round 1 ideas.
5. **Round 1 ends** in one of two ways:
   - A finalist passes Stage 2 and the red team. Go to pre-registration.
   - Every Round 1 idea is killed or parked. Report, and propose Round 2 with its data
     plan for Richard's approval.

## Round 2: new data (only after Round 1 reports)

- **Pull one full day first,** and measure its real size and time.
- **Then the lean pull:** SPXW at every expiration out to about 45 days; every minute for
  0 to 2 days to expiration, and 15-minute samples beyond, if the vendor's interval allows
  it; a strike band that drops contracts no one would trade.
- **Other underlyings only when the map says they are worth it.**
- The same funnel, the same ledger. The search count carries over.

## The funnel and kill rules

Kill rules are written **before** each stage runs, and each stage costs more than the one
before.

| Stage | Cost | Passes only if | Survivors |
|---|---|---|---|
| **0. Mechanism and ceiling** | minutes | It names who pays and why that persists, AND its ceiling (the map's generous upper bound: fills at the mid, perfect timing, no stops) is at least 2× its round-trip cost | top ~20 (the cap) |
| **1. The simplest version** | ≤ 2 hours each | After-cost mean > 0 at a loose bar (t ≥ 1.5), positive in ≥ 3 of the 4 development years (2020 to 2023), and the best 5 days are less than half its total | top ~5 |
| **2. Robustness** | ≤ 1 day each | ≥ 70% of neighbouring parameter cells also profit after costs (a plateau, not a spike), break-even h ≥ 0.5, and the development years' stress (2020-02-24 to 03-31, 2022, 2023-03-13) stays inside the loss budget at the proposed size | top 1–3 |
| **3. Red team, then pre-registration** | days | The red team cannot break it | finalists (≤ the finalist cap) |

- **Time-boxes.** An idea that spends its stage budget without a clear answer is parked
  as inconclusive, and the search moves on.
- **No rescue tweaks.** A variant is allowed only when the idea's own mechanism motivates
  it, never the last result. At most 5 variants per idea at Stage 1, every one in the
  ledger. A killed idea comes back only with a new mechanism argument, written down before
  it is run.
- **Branch-deciding questions first.** Answer a question that reorders the whole tree
  (overnight against intraday is one) before testing any single strategy.
- **The kill rate is a health check.** Expect 80–90% of ideas to die at Stages 0 and 1.
  If they do not, the gates are too loose.
- **The cap binds.** If nothing clears Stage 2 within a round, report "nothing found" for
  that round. Do not widen the search to rescue it: every variant tried raises the bar the
  finalists must clear.

## Working in parallel without fooling ourselves

- Families run in parallel through subagents that share **one engine, one ledger and one
  set of gates.**
- **One integrator (you) keeps the scoreboard:** each idea's stage, status, ceiling,
  after-cost mean, budget spent and next step.
- **A subagent never promotes its own idea.** The integrator applies the written rule.
- **The red team is a separate agent** from the one that built the idea.
- **A daily summary to Richard, a few lines:** what was killed and why, what advanced,
  and what is next.

## Costs and statistics

- **The day is the unit of evidence.** A family's best variant is judged against the
  whole search: a max-statistic permutation over every variant in the ledger (White's
  reality check or Hansen's SPA), or a deflated Sharpe ratio. Never judge it alone.
- **h = 1/3 is a placeholder.** It was measured on 22 fills of one structure. Report
  every result at h = 0, 1/3, 1/2 and 1, and its break-even h.
- **Fees are each product's real schedule. Settlement is the official close.**
- **Stress.** In development, show every candidate through 2020-02-24 to 03-31, 2022 and
  2023-03-13. The locked years' crashes (2024-08-05, 2025-04-07 to 10) are shown only in
  a finalist's confirmation report, and never looked at before it. A premium-selling idea
  that looks smooth is usually selling crash insurance. Show the crash.
- **Capacity.** At 2×, 5× and 25× size, compare the order with the quoted `bid_size` and
  `ask_size`, and say what does not fit.

## The red team

A separate agent tries to break each survivor. It checks for:

- look-ahead bias, stale or crossed quotes, and fills at prices no order could get;
- settlement errors, early-close days, holidays, missing sessions;
- dividends and rates in the parity forward;
- a mechanism the market structure has since changed (daily expiries from 2022, the 2023
  retail boom);
- crowding;
- an edge that lives in one year or one event.

Survivors are only what it could not break.

## Pre-registration and confirmation

- For each finalist, within the finalist cap, write a pre-registration in the
  style of the 2026-09-30 one: a quantitative spec, a frozen scorer, the pass criteria and
  what a pass allows.
- Richard signs off and commits it. **Only then are the locked years scored, once.**
- A pass allows a design record. Then the phase rule applies: shadow, then live at
  minimum size, then scale.
- **Every rail in `CLAUDE.md` holds for implementation.** An idea that needs broker-side
  stops, legged entries or undefined risk is unavailable unless Richard changes that rail
  himself.

## Reporting

- **Plain terms first, in every report.** Richard is fluent in market structure, but asks
  for the non-trader version before any decision.
- **Numbers:**
  - dollars a session at one unit;
  - then a year, as a percentage of the account, at the proposed size;
  - always beside the day SD, the worst day and the drawdown.
- **Findings go in files, progress in short lines.** The only stops are Checkpoint 0 and
  each pre-registration sign-off.

## Deliverables

- In `docs/research/<date>-edge-discovery/`:
  - `PLAN.md` and `STATE.md`;
  - the engine and its tests;
  - the map;
  - the inventory and the scoreboard;
  - the ledger;
  - one short note per screened family, including the failures;
  - the red-team findings;
  - the finalists' pre-registrations.
- A results note for each round, indexed in `docs/research/README.md`.
