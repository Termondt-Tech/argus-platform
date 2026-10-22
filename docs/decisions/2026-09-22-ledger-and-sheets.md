# The trading ledger, reconciled against the broker, in a Google Sheet (2026-09-22)

*Argus has a nightly report that answers "what happened today?" and nothing that
answers "what has this made, net of fees, this week / month / year?". The EOD
transcript is per day and prose, `docs/reconciliation-live.txt` has stopped
growing on purpose, and the broker's own records have never been compared to
Argus's by anything but a human reading two screens.*

> **SHAPE AGREED 2026-09-22; BUILT AND DEPLOYED the same day — TAGGED AND RELEASED
> AS v0.4.7 (Kochab), 2026-09-22 after the close.** → [release notes](../releases/v0.4.7.md).
> Choices made by Richard in
> conversation: **a Google Sheet** as the surface (report tabs and charts built
> there by hand, over data tabs a machine owns); **the broker import from day
> one**; **built on the droplet**, not the NAS (the NAS stays a dumb archive, so
> replacing it changes nothing here); **the last step of the existing EOD job**,
> not a new unit; **OAuth as Richard with the `drive.file` scope**, which needs no
> crypto library and so adds no dependency and no second image.
>
> **Revised the same day.** The first draft had a separate timer, a second Docker
> image and a Google service account. The service account was the cause: its login
> signs a JWT with an RSA key, the stdlib cannot sign RSA, and the runtime is
> stdlib-only apart from the tastytrade SDK. So it needed a Google library, which
> needed its own image to keep the trading image clean. An OAuth refresh token is
> exchanged for an access token by a plain HTTPS POST, which `urllib` does, and
> `drive.file` restricts it to files the app itself created. That is the same reach
> the service account would have had, without the image, the dependency or the unit.

## What this is, in one sentence

After the EOD report is delivered, one more read-only container rebuilds the whole
live ledger from `argus-live.db` and the broker's transaction history, reconciles
the two per broker order to the cent, writes CSVs beside the EOD transcripts (where
the NAS already collects them), replaces the data tabs of one Google Sheet, and
exits.

---

## What the evidence already settled

Three broker artifacts for 2026-09-17, -18 and -21 were checked against Argus
before anything was designed.

**1. Argus and the broker agree to the cent on all three days.**

| Day | Broker (confirmation) | Argus |
|---|---|---|
| 09-17 | 601.12 − 520.88 = **80.24** | `desk` total 80.24 |
| 09-18 | 615.12 − 524.88 = **90.24** | `desk` total 90.24 |
| 09-21 | 4,665.60 − 5,444.40 = **−778.80** | five spreads rebuilt from fills: −730.00 gross, −48.80 fees |

So reconciliation is at the cent, never within a tolerance. A tolerance would
quietly absorb exactly the defects this exists to find.

**2. The fee schedule, per leg fill of 2 contracts:** commission **$2.00 on
OPENING legs only** (closes are free), a $0.04 transaction fee, and $1.40 of
additional fees ($0.70 a contract: clearing and the SPX index-option fee). A
spread closed intraday costs **$9.76**; a spread held to expiry costs $6.88. At
minimum size fees are ~10% of a winning day's gross, which is why fee drag is a
first-class column and not a footnote.

**3. `desk.read_fees` has held up on one repricing day, and that is all.** It sums
`fees` off every `order.submitted`, and a reprice submits a new order per rung, so
the worry was a double count. On 2026-09-21 two entries repriced once
(`call-7745-7750`, `call-7755-7760`, `entry_steps = 1`) and Argus's day total,
−48.80, still matched the broker to the cent. One day and two single-step walks
settle nothing about a longer walk. **This ledger does not use Argus's fee figure as
the fee.** The broker's is the fee; Argus's is carried beside it as a check.

**4. Neither broker download is an input.** The confirmation PDFs are the
clearing firm's document: per leg fill, no broker order id, execution time on
some lines only, and a CUSIP that changes with the position's side (7760 appears
under two). The Activity CSV is per ORDER with `Order #`, but carries no fees, no
settlement, no year, and Central times. Both stay as occasional spot checks. The
input is the API.

**5. The API has every field the downloads lack.** `Account.get_history`
(`GET /accounts/{n}/transactions`, already in the pinned SDK) returns
`Transaction` records with `order_id`, the fees broken out (`commission`,
`clearing_fees`, `regulatory_fees`, `proprietary_index_option_fees`,
`other_charge`), `is_estimated_fee`, `transaction_type`/`transaction_sub_type`,
`executed_at` in UTC, and `value`/`net_value`.

**6. The join is exact, not fuzzy.** Every `order.fill` payload carries
`broker_order_id` (`AppliedFill.to_payload`). So Argus and broker are matched on
`order_id`, never on strike + time + price.


**7. Step 1 was run against the live snapshot `argus-20260921T163001.db`**
(read-only, a scratch config pointing at the NAS copy in `dev/nas/`). Every number
the confirmations carry came out: gross 90.00 / 100.00 / −730.00, Argus fees −9.76 /
−9.76 / −48.80, and all 14 broker order ids equal to the Activity export's
`Order #`. It also showed Argus records a fill per SLICE (49 `order.fill` rows where
the confirmations print 28 leg lines), which is why reconciliation is per ORDER.
---

## The shape

```
argus-eod.timer 16:45 ET
  └─ argus-eod.sh
       ├─ the EOD report, rendered and delivered           (unchanged)
       └─ LAST: python -m argus.tools.ledger --push        (one-shot, seconds)
            ├─ reads argus-live.db, mode=ro
            ├─ one read-only broker GET  (get_history)
            ├─► /path/to/backups/reports/ledger/*.csv   ──► NAS (existing pull)
            └─► Google Sheets API: replace the data tabs
```

**Nothing runs continuously.** The ledger is a `docker compose run --rm`
container, the same kind as the report. It starts, works for a few seconds, and
exits.

**The last step of `argus-eod.sh`, not a section of `tools.eod` and not its own
unit.** It runs AFTER the report is delivered, so a Google outage, a revoked token
or a broker 502 on the transactions call cannot cost the report. Its failure pages
the report channel with one line and does not change whether the report counted as
delivered. Being inside the existing script also means no new unit, so there is
nothing new for the deploy classifier or `tests/test_deploy_wiring.py` to learn
beyond one wiring test that the step exists and runs last.

### The whole ledger is rebuilt every night

Not appended, not upserted. Every run recomputes every trading day since the first
session in `argus-live.db`, fetches broker history over the same range, and
**replaces** the data tabs.

- **Idempotent by construction.** A re-run, a catch-up after a missed night, or two
  runs in one evening produce the same Sheet. No row keys to keep consistent, and
  no duplicate rows.
- **Corrections flow in for free.** A day that was `PROVISIONAL` (fees still
  estimated, settlement not yet posted) becomes `OK` the next night without any
  "update the old row" logic.
- **Size is not a concern.** A handful of rows a day: years of history are
  thousands of rows, and one paginated transactions call.
- **The report tabs survive it.** They reference the data tabs by whole column
  (`days!A:A`), so replacing the values keeps every formula and chart valid.
  The one rule for whoever builds them: **never type into a data tab.** It is
  overwritten nightly.

### `tools.ledger`

Read-only, `mode=ro`, no lock. **It is not a new scorer**:

- Per day it calls `desk.load` + `desk.summarize`, the same objects the EOD digest
  reads, and a trade's gross IS `ClosedPosition.pnl`. The fills come from
  `shadow.marker.read_fills`. To carry the join key, `marker.Fill` gains
  `broker_order_id: int | None = None`, additive, so there is still exactly one
  decoder of `order.fill`.
- It reuses `eod.read_g1` and `eod.read_blackout_day` for NLV and blackout, and
  `eod.latest_settled_session` for the last day to include.
- **Money is written as fixed two-place strings.** The shared scorer is float;
  every price is a 0.05 tick times 100, so rounding to the cent at the boundary is
  exact in practice, and it happens in ONE function. The broker side and the
  reconciliation compare in `Decimal`.
- Step 2 adds ONE trading session and ONE paginated `get_history` call. It sends no
  orders and nothing that mutates. It never retries the login (ban risk, the
  `_login_with_retry` rule). A failed fetch still writes the Argus-side CSVs and
  marks every day `BROKER_UNREAD`.

### Data tabs (machine-owned, replaced nightly)

**`argus_fills`**: one row per `order.fill`.
`trading_date, time_et, candidate_id, broker_order_id, direction, buy_sell,
symbol, side, strike, contracts, price, cash`

`buy_sell` comes from the cash sign, never from the broker's leg action, which it
relabels on a chained book (2026-08-25).

**`trades`**: one row per trade idea (`candidate_id`).
`trading_date, candidate_id, side, short_strike, long_strike, width, contracts,
entry_time_et, entry_credit, entry_steps, entry_limit, exit_time_et,
exit_trigger, exit_outcome, exit_debit, outcome, gross, max_loss,
gross_pct_of_max_loss, seconds_since_prev_exit, delta_source, version,
entry_order_ids, exit_order_ids`

- `outcome` is `closed`, `held_to_expiry`, `partly_closed` or `unpriceable`.
- `max_loss` = (width − entry_credit) × contracts × 100. The percentage keeps
  results comparable when size scales.
- `seconds_since_prev_exit` exists because 2026-09-21 re-entered one strike higher
  2, 10, 25 and 3 seconds after its four stops. Nothing constrains that today, and it
  should be visible whether or not anything ever does.
- `delta_source` and `version` are there because pooling call outcomes across
  v0.4.6 is refused (mid-implied delta record). `delta_source` is read off the
  trade's last `signal.candidate`; `version` off the last `process.start` before
  its first fill.
- **A held-to-expiry trade's `gross` is blank, never `0`, until the broker's
  settlement record for it exists.** A blank reads as unknown. A zero reads as a
  scratch. Step 2 adds `fees`, `net` and `recon` from the broker.

**`days`**: one row per ET trading date, including days with no trades, starting
at the first session whose close came after the database's first `process.start`
(so the cutover day, 2026-09-15, which ran in cert, is not a zero row).
`trading_date, blackout, entry_attempts, trades, closed, held_to_expiry,
unpriceable, gross, argus_fees, nlv_at_first_gate, nlv_at_last_gate, gate_rows,
version`

`gross` is blank when any trade that day is still unknown. The NLV pair is read
off the first and last `gate.decision`, NOT the open and close: the gate writes only
while candidates are evaluated, so on 2026-09-17 the last reading predates the
15:31 flatten and the pair differs by 93.12 against a day's net of 80.24.
`version` is the build running at the day's CLOSE; an evening deploy is the next
session's build. Step 2 adds
`broker_fees, broker_net, net, recon`.

**`broker_txns`** (BUILT 2026-09-22): one row per `Transaction`, unchanged apart from types.
`id, order_id, time_et, transaction_date, type, sub_type, symbol, action,
quantity, price, value, commission, clearing_fees, regulatory_fees,
index_option_fees, other_charge, fees_total, net_value, is_estimated_fee,
description`

**`recon`** (BUILT 2026-09-22): one row per trading date, the checks behind `days.recon`.
`trading_date, argus_gross, broker_gross, gross_diff, argus_fees, broker_fees,
fee_diff, argus_order_ids, broker_order_ids, unmatched_argus, unmatched_broker,
estimated_fee_rows, settlements_pending, status`

### Reconciliation status

Per day, first match wins:

| Status | Means |
|---|---|
| `BROKER_UNREAD` | the transactions fetch failed tonight; Argus side only |
| `DIFF` | any order id on one side only, or gross/fees differ by one cent or more |
| `PROVISIONAL` | agrees so far, but a fee is still `is_estimated_fee` or a held-to-expiry settlement has not posted |
| `OK` | every order matched, gross and fees exact, nothing estimated, nothing pending |

**Broker gross, not Argus gross, is what the headline P&L sums**, because the broker
is the source of truth. Argus's gross sits beside it as the check. A `DIFF` is a
finding to chase, never something to average.

**A broker record with no Argus order id** (a manual close, anything foreign) is
`unmatched_broker` and makes the day `DIFF`. The account is dedicated to Argus
(non-negotiable), so a foreign transaction here is itself worth a page.

**Paging.** A failed ledger run posts one `:warning:` to the REPORT webhook (a
Sheet that did not update is not a trading event). A `DIFF` day adds one line
there. `PROVISIONAL` never pages: it is the normal state of a same-day row.

---

## The report tabs (BUILT and VERIFIED LIVE 2026-09-22)

`python -m argus.tools.sheets report` builds four TOOL-OWNED tabs — `Dashboard`,
`Weekly`, `Monthly` and a hidden `calc` — and a rebuild replaces exactly those four.
A tab the module does not name is never touched, so the operator's own work is safe.

**Run by hand, never nightly, and everything on them is a FORMULA over the data
tabs.** So the views are current the moment a push lands, nothing scores the P&L a
second way, and any number is traceable to the row it came from. `calc` carries the
chart series (daily net, cumulative, running peak, drawdown) and is hidden because it
is machinery; charts read a hidden sheet perfectly well.

**Four defects found by reading the computed cells back off the real spreadsheet,
each of which rendered a PLAUSIBLE WRONG NUMBER rather than an error** — the reason
this was verified live rather than only unit-tested, and each now has a test:

- **`SUMIFS` is not array-aware.** Inside `ARRAYFORMULA` it returns the first row's
  answer repeated down the column, so every week showed week one's P&L. The rollups
  use `MAP`/`LAMBDA`. `COUNTIFS` happened to work and is written the same way anyway.
- **`QUERY(..., 1)` over a range starting at row 2 ate the first TRADE as a header.**
  2026-09-17 vanished from every grouping while the KPI count still said 7 trades —
  a disagreement between two cells on one page, which is exactly the shape of failure
  this project keeps designing against.
- **Two spilling `QUERY` blocks sharing rows render `#REF!`** with nothing saying
  why, so each block owns its rows with room to grow.
- **Number formats are per CELL BLOCK, never per column.** A win rate in a money
  column reads `$0.29`; a trade count reads `$5.00`; a `QUERY`'s date column reads
  `46286` unless the region it spills into is formatted.

**A PERIOD FILTER on the dashboard** (2026-09-22): a strict dropdown in `B3` —
`All`, `YTD`, `This month`, `This week`, `Last 30 days`, `Custom` (with `E3`/`F3`
dates) — resolved ONCE into `calc!H2`/`H3`, which every KPI, every grouping and the
chart series read. One window in one place, so a KPI cannot be left showing a
different period from its neighbour; a test fails on any whole-column aggregate that
does not read it. **The subtitle names the window that is showing** (`Showing 17 Sep
2026 → 18 Sep 2026 · 2 sessions · 2 trades`), because a filtered dashboard that looks
like an all-time one is the quiet way to misread a number. **The window is anchored on
the LAST SESSION IN THE LEDGER, never the clock**: on a Saturday, or before a push, a
calendar-based "this month" would silently exclude the newest day the ledger holds.
Every fallback — unknown period, empty custom date — resolves to the whole ledger.

Two more failures worth carrying, both found live and both now tested: a `QUERY`
`where` clause needs **date LITERALS** built by string concatenation
(`" & " and A >= date '" & TEXT(...)`), and **the `&` that rejoins the string is
easy to drop** — without it the formula reads as `"…"` followed by a bare `and`, which
renders `#ERROR!` with no hint.

**Verified against the live spreadsheet**: net −$608.32 across 4 sessions, which ties
to the three broker confirmations, with no error cell on any tab — and the filter
exercised across All / This week / Custom, each total re-deriving correctly.

---

## The broker side, as built (2026-09-22)

`tools.broker_ledger`, reached by `tools.ledger --broker` (OFF by default, so a local
run needs no credentials and touches no network). One paginated read-only
`Account.get_history`; an AST test pins that the module cannot reach the submission
path and that the SDK import lives INSIDE `fetch_history`, so the pure functions and
every run without `--broker` need no session at all.

- **The broker's numbers are THE numbers**; Argus's gross and its `order.submitted`
  fee estimate sit beside them. Matched per `order_id`, compared in `Decimal`, **to
  the cent with no tolerance** — a tolerance absorbs the defect this exists to find.
- **`days` and `trades` gained broker columns at the END** (`broker_fees`,
  `broker_net`, `net`, `recon`; `fees`, `net`, `recon`), so no existing spreadsheet
  column moved and the dashboard's formulas were untouched.
- **A trade whose orders the broker has not ALL reported keeps a blank fee and a
  blank net.** A partial answer is not a fee.
- **A failed fetch is not fatal and blanks nothing**: every day reads
  `BROKER_UNREAD`, the Argus side is written as usual, and the next rebuild picks the
  broker up. Without the `fetched` flag a failed fetch and a genuinely empty day (a
  blackout) would be the same empty list.
- **A broker row with no `order_id` is `unmatched_broker` and makes the day `DIFF`**,
  named by its description. That covers a cash settlement (so a failed flatten is
  loud) and anything foreign (the account is dedicated to Argus).
- **`broker_txns.csv` and `recon.csv` are written only when the broker was asked.**
  A header-only file would push an empty tab over yesterday's, which reads as "the
  broker reported nothing" rather than "nobody asked".
- **The dashboard gained a reconciliation view**: `Days reconciled with the broker`,
  `Days needing a look` (everything not OK and not blank — the operative question is
  "is there anything I have not seen?"), and a `NOT RECONCILED WITH THE BROKER` block
  listing the offending days with their diffs.

**One real defect fell out of the first push after this change, and it had been
there since the push was written**: the grid-grow request identified its tab by
TITLE, which `updateSheetProperties` ignores — it addresses `sheetId` only, so the
resize had been landing on sheet 0 (`Sheet1`) and started failing outright once that
tab was deleted. It now resolves each tab's real id, refuses to resize a tab whose id
it does not know, and the fake in the tests asserts the id is present.

**RUN AGAINST THE LIVE ACCOUNT 2026-09-22 and it reconciles: 4 days, all `OK`.**
Gross 0 / 90.00 / 100.00 / −730.00, fees 0 / −9.76 / −9.76 / −48.80, broker net
80.24 / 90.24 / −778.80 — every `gross_diff` and `fee_diff` exactly `0.00`, no
unmatched order on either side, 50 broker rows against 49 Argus fills (the broker
slices differently; the join is per ORDER, which is why that does not matter).

**The live read overturned one design assumption: `is_estimated_fee` is set on EVERY
row**, including 2026-09-17's, whose fees match the clearing firm's confirmation to
the cent. So it does not mean "not yet finalised", and gating `PROVISIONAL` on it
made `OK` unreachable — the first run reported `1 OK, 3 PROVISIONAL` for three days
that agreed perfectly. It is now recorded as a column and a count that **nothing
decides on**, which is this project's usual answer for a field whose semantics are
unverified. `PROVISIONAL` now means what it should: a trade whose result is not known
yet.

Two smaller findings from that run: **`tools.ledger` never called `load_env`**, so
the first `--broker` attempt failed on missing credentials and demonstrated the
`BROKER_UNREAD` path end to end (the Argus side wrote, nothing was blanked); and the
env load now lives inside the broker path, so a run without `--broker` still needs no
credentials at all.

## Google auth: a service account, and the OAuth dead end

**A SERVICE ACCOUNT, with the spreadsheet shared to its `client_email`.** Richard
creates the sheet and shares it as Editor; the account holds NO project role, so the
key reaches nothing else in Google Cloud and nothing else in the Drive. No consent
screen, no publishing status, no domain, no token expiry.

**OAuth was tried first and abandoned, 2026-09-22 — record it so nobody re-treads
it.** The attraction was that a refresh-token exchange is a plain POST and needs no
crypto library. But an External OAuth app cannot leave "Testing" without a home page
URL, a privacy policy URL, authorized domains (a domain you own) and a logo, and
changing those triggers brand verification; `drive.file` being a non-sensitive scope
buys no exemption. In "Testing", refresh tokens expire after 7 days, which breaks a
nightly job every week. So OAuth means owning a domain and publishing a privacy
policy for a one-person ledger. **Cost of getting there: two wrong instruction sets
handed to Richard and a reversal of a settled recommendation — the source of that
mistake was giving console steps and platform rules from memory instead of checking
the vendor's current documentation.**
→ [Manage OAuth App Branding](https://support.google.com/cloud/answer/15549049),
[publishing status](https://support.google.com/cloud/answer/15549945)

**Workload Identity Federation, which Google's console recommends instead of a
downloaded key, does not apply here.** It replaces a key with an identity the
platform already issues; a DigitalOcean droplet has none, so using it would mean
being our own OIDC issuer — signing identity tokens with our own key on that same
droplet and uploading a JWKS to Google. Same key exposure, same RS256 code, plus a
pool, a provider and a JWKS rotation. Recorded so the console's warning does not
reopen this.

**The signing is ours: `tools.rs256`, stdlib, ~50 lines** (PEM → DER → PKCS#1 v1.5
padding → one `pow()`). Hand-written crypto needs its justification stated, and the
module's docstring carries it: it only SIGNS (no verification path to bypass), a bug
is a REFUSED login rather than a weakened one, the `pow()` timing channel needs local
measurement by someone who could read the key file anyway, and **every signature is
pinned byte-for-byte against `openssl dgst -sha256 -sign`** in `tests/test_rs256.py`
(PKCS#1 v1.5 is deterministic, so that comparison is exact). The alternative was a
dependency in a stdlib-only image, or the second image that dependency would have
forced.

**The scope is `spreadsheets`, and the fence is the SHARE, not the scope.** A
service account's token carries the scope but reaches only files shared with it.

**The key file lives at `/etc/argus/argus-sheets-key.json`, owned by uid 10001, mode
400** — 10001 because the container runs as that uid, not root (an earlier draft said
root:600, which the container could not read). **Never in `.env`**, the trading
container's `env_file`, so it never enters the trading process's environment or
`docker inspect`. Mounted read-only into the ledger run alone. Never in
`/path/to/backups`, so it never reaches the NAS. `.gitignore` names
`argus-sheets-key.json`, `*service-account*.json` and `client_secret*.json`, so a
copy outside `dev/` cannot be committed.

**If the key leaks, the blast radius is one spreadsheet** — whose contents are
re-derived from the audit log nightly. Revocation is deleting the key, unsharing the
sheet, or disabling the account; any one is enough. Rotation is: new key, replace the
file, delete the old.

**VERIFIED AGAINST REAL GOOGLE 2026-09-22**: `check` signed in as the service
account, created all five data tabs in Richard's spreadsheet and confirmed write
access — so the stdlib RS256 assertion is accepted by Google's token endpoint in
practice, not only byte-matched against `openssl`.

**`python -m argus.tools.sheets check --key … --spreadsheet-id …` (BUILT
2026-09-22)** is run on the Mac before anything is deployed: it signs in, creates any
missing data tab, and **proves EDITOR access even when no tab is missing** by setting
the title to what it already is — otherwise `check` would pass on a read-only share
and the first push would fail. It is its own module and takes no `--config`, because
that rule exists to stop a CWD-relative `db_path` opening the live database and this
module imports no `argus` runtime, no `sqlite3` and no `tastytrade` (an AST test pins
it).

---

## Rails

- **Never write to either database, never take the writer lock.** `mode=ro`,
  always.
- **The broker call is `get_history` and nothing else.** No order endpoint, and
  an AST test pins that `tools.ledger` imports nothing from `execution/`'s
  submission path.
- **Never let a ledger failure reach trading or the EOD report.** It runs after
  delivery, in its own container, with its own failure line.
- **The Google key is never in `.env`**, is mounted only into the ledger run, and
  never lands in `/path/to/backups` (which the NAS pulls).
- **`tools.rs256` signs and nothing else.** Never grow a verification path there:
  the justification for hand-written crypto depends on it.
- **Never parse another program's prose.** The ledger reads the chain and the API,
  never the EOD transcript.

---

## What the build still has to confirm

1. **The API sample.** One `get_history` response for 2026-09-17..21, read-only
   against the live account. It confirms the field names and whether same-day fees
   arrive `is_estimated_fee = true`. It becomes a committed test fixture
   (account-scrubbed).

**Held to expiry is a FAILURE path, not a gap in the data** (Richard, 2026-09-22).
The 15:30 flatten stays on, so a spread reaches expiry only when the flatten fails:
unpriceable, exits spent, or a broker outage like 2026-09-14. Step 2 therefore does
NOT parse settlement records ahead of time. A cash-settled expiry most likely
arrives with no `order_id`, which the reconciliation already reports as
`unmatched_broker`, so the day is `DIFF`, loud, with the broker's own description
in `broker_txns`. That is the right answer for a failed flatten. The EOD report
already says `HELD TO EXPIRY` the same evening. If it ever happens, that day's real
record becomes a fixture and settlement gets its own handling from a real example.

## What only Richard can do

1. In Google Cloud: a project, **APIs & Services -> Library -> Google Sheets API ->
   Enable**, then **IAM & Admin -> Service Accounts -> Create service account**
   (no project role), then its **Keys -> Add key -> Create new key -> JSON**. The
   key downloads once. **DONE 2026-09-22.**
2. Create the spreadsheet, **Share** it as **Editor** with the key's `client_email`
   (untick "Notify people"), and take its id from the URL.
3. On the Mac: `python -m argus.tools.sheets check --key dev/argus-sheets-key.json
   --spreadsheet-id <id>`.
4. Install the key and the spreadsheet id on the droplet (README §4, "The trading
   ledger and the Google Sheet"): `/etc/argus/argus-sheets-key.json` (mode 400, uid
   10001) and `/etc/argus/ledger.env` holding `ARGUS_LEDGER_SHEET=<id>`. Both are per
   host and neither is tracked, like `host.env`.
5. Copy the updated `scripts/argus-pull.sh` onto the NAS (runbook §13) so the ledger
   arm runs there. Until then the CSVs live on the droplet only.

## Build order

1. **`tools.ledger`, Argus side only**: `argus_fills`, `trades`, `days` CSVs,
   tested on fixture DBs. No network.
2. **`tools.rs256` + `tools.sheets check`** (BUILT 2026-09-22, ahead of step 3 so the
   Google setup can happen in parallel with step 2).
3. **Broker side** against the SDK `Transaction` model: `broker_txns` and `recon`,
   tested with `Transaction`s built from the confirmation PDFs. Estimated-fee
   handling is isolated in one function until the API sample confirms it.
4. **The Sheets push**, stdlib `urllib`, tested against a fake endpoint. **DONE.**
5. **The `argus-eod.sh` step**, plus its wiring tests. **DONE 2026-09-22** — the LAST
   step of the existing job, after delivery, and **it can never fail the unit**: a
   non-zero exit from `argus-eod` means one thing (the digest did not arrive) and that
   sentence has to stay unambiguous, so a Google outage warns on the REPORT channel
   and leaves the exit code alone. Every invocation sits inside an `if`, because under
   `set -e` plus the ERR trap a bare failing command would page as a JOB failure and
   exit before retention ran. The Google key is mounted read-only into the PUSH alone
   — the ledger build reads the trading database and has no business holding an
   external credential — and a missing key or spreadsheet id is a SKIP with the CSVs
   still written, on the missing-webhook rule. A third arm in `scripts/argus-pull.sh`
   mirrors the CSVs to the NAS, after the snapshot and transcript arms and independent
   of both. Six wiring tests pin all of it.
