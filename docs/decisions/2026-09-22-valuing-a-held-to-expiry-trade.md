# Valuing a held-to-expiry trade in the ledger (2026-09-22)

*The ledger's first live night found the hole it could not have found in tests.
2026-09-22 opened one put spread (`20260922-put-7740-7735`, 2 contracts, 0.50
credit, entered 10:00:00 — the first trade ever selected on mid-implied delta) and
**never closed it**: at 15:30 the flatten found no market, so it refused to invent a
price and the spread expired. That is the flatten's documented modal regime on a
winning day, not a malfunction ([the exit ladder](2026-08-31-exit-ladder-near-tick.md)).*

*The ledger reported the day as `PROVISIONAL` with a BLANK gross, which is honest —
and permanent. A 0DTE expiry produces no fill to price from, and an OTM expiry
produces **no broker transaction either**, because only ITM options cash-settle. So
nothing would ever arrive to fill the blank, and the Dashboard's headline Net P&L
would silently exclude a **+$93.12** day. The design's `DIFF`-on-settlement path
covered the ITM case and missed the OTM one, where the result is knowable but
unreported.*

> **DESIGNED 2026-09-22; BUILT 2026-09-23, DEPLOYED IN v0.5.0 (Vindemiatrix, 2026-09-23 — see
> [release notes](../releases/v0.5.0.md)).** Both open questions
> were put to Richard and both took the recommendation: **the price comes from the
> last recorded spot in the chain**, with a persistent override file for an official
> print; and **a settlement-valued day COUNTS in the headline Net P&L**, labelled,
> with its own KPI. `src/` + `scripts/` + tests + docs; no config key, no migration,
> no audit category. **Deploying it needs one manual step no image carries: re-run
> `tools.sheets report`**, because the Dashboard's formulas changed and the nightly
> push never touches the report tabs.
>
> **Two places the build departs from the text below, both forced by the
> acceptance numbers** (see "As built" at the end): `days.gross` is the day's FILL
> CASH rather than the realized P&L of closed trades, and `days.total_gross` is the
> sum of every trade's gross rather than `gross + settled_gross`. And a refusal the
> text did not name got its own basis, `no_spot`.

## What this is, in one sentence

A held-to-expiry spread's terminal value is computed from the settlement price with
`shadow.outcome.settlement_liability` — the scorer `tools.mark` already uses — and
lands in its own columns with its basis named, while the reconciliation against the
broker keeps comparing FILLS only.

---

## Decisions (all settled)

**1. The settlement price is the day's LAST RECORDED SPOT, from the chain.**
`watchdog.check` carries `spot` on every tick (`watchdog.py`'s evaluation payload),
and a held position is evaluated until the session ends — so the last such row is
within a minute or two of the close. Verified on the 2026-09-21 snapshot: 9,014
`watchdog.check` rows carry `spot`, the last at 12:32 ET (that day's final exit).

- **No network, and re-renderable.** The ledger already opens no market-data
  session; this keeps it that way, and it means any past day rebuilds to the
  IDENTICAL number. `tools.mark --from-feed` cannot do that — it reads the current
  quote, so a re-render weeks later scores a different close.
- **It is a PROXY, and the record says so.** SPXW is PM-settled on the official Cboe
  print, which is not the last streamed index tick. `outcome.py`'s own docstring
  already carries that caveat. The basis column names which number was used.
- **Read from `watchdog.check` AND `watchdog.check_spent`**, newest by `seq` — the
  same `MARK_CATEGORIES` pair `desk.read_marks` reads, for the same reason: a spent
  exit moves a position's marks to the second category and never back, so reading
  only the first freezes at the moment the exit died (2026-09-14).

**2. A recorded spot too far from the close does not get used.**
`SETTLEMENT_SPOT_MAX_AGE_MIN = 15`, a module constant (the `BLACKOUT_WARN_DAYS`
precedent — a new REQUIRED config key would force a coupled deploy for a reporting
nicety). If the newest spot-bearing row on that date is more than 15 minutes before
`MarketCalendar.close_time(d)` (half-day aware, never a literal 16:00), the trade is
left UNVALUED with `gross_basis = spot_too_early`. A spread held to expiry is watched
to the close, so this should never fire; when it does, something else is wrong and a
guessed settlement would hide it.

**3. An official print overrides it, and the override PERSISTS.**
`/etc/argus/settlements.csv`, per host, untracked, `date,price` one per line,
`#` comments allowed. A date present there wins, and the row reads
`gross_basis = settlement_official` instead of `settlement_spot`. It has to persist
because **the ledger rebuilds every day on every run**: a one-off CLI value would be
silently replaced by the recorded spot the next night, and a hand correction typed
into the Sheet would be overwritten by the next push.

- `tools.ledger --settlements <path>` (optional; no default path inside the
  container, because `/etc/argus` is not mounted there).
- `argus-eod.sh` mounts it read-only **only if the host file exists**
  (`ARGUS_SETTLEMENTS`, default `/etc/argus/settlements.csv`), on the same
  "absent is a skip, not a failure" rule as the Google key.
- **A malformed line costs its own DATE, not the run**: that date is left unvalued
  with `gross_basis = settlement_file_error` and a log line. Refusing the whole run
  would cost every other day's CSVs for one typo.

**4. A settlement-valued day counts in the headline Net P&L, labelled.**
Otherwise the Dashboard understates a winning month every time the flatten cannot
find a market — which is that flatten's normal outcome. The label is not optional:
a KPI says how much of the total is not fill-derived, and the trade row names its
basis.

**5. The reconciliation keeps comparing FILLS ONLY, and this is the load-bearing
rule.** The broker has no row for an OTM expiry, so a settlement-derived number in
`broker_gross`'s comparison would make every such day `DIFF` for a difference that
is CORRECT. `days.gross` stays fills-only and remains the recon input; the settlement
value rides in its own column.

**6. `PROVISIONAL` regains its meaning.** It is a trade valued by NEITHER fills nor
settlement (`spot_too_early`, `settlement_file_error`, or an unpriceable execution) —
not "a result nobody went to fetch". A day whose expiry is valued reaches `OK`.

---

## The shape

### `tools/settlement.py` — its own module, not folded into `broker_ledger`

The broker module is about what the BROKER said; this is about what the index did.
Keeping them apart is what stops a settlement estimate drifting into the broker
comparison, which rule 5 above forbids.

Pure functions, no I/O:

```python
read_settlement_spots(conn, trading_date) -> tuple[float, datetime] | None
    # newest `spot` across MARK_CATEGORIES on that ET date, with its timestamp

read_settlement_overrides(path) -> dict[date, float | None]
    # None = that line was malformed; the date is then refused, not skipped silently

settle_trade(side, short_strike, long_strike, contracts, entry_credit, settlement)
    -> tuple[float, str]
    # (gross dollars, result) where result is outcome.classify(...)'s
    # full_credit | partial | max_loss
```

**`settle_trade` MUST call `shadow.outcome.settlement_liability` and
`shadow.outcome.classify`.** It re-implements neither. Gross is

```
(entry_credit - liability) * contracts * SPX_MULTIPLIER
```

which is `Outcome`'s own second term (`outcome.py`'s "settlement on the contracts
HELD"), expressed on the REALIZED credit the ledger already carries.

### Columns (appended at the END of each row type)

`trades`: `gross_basis`, `settlement_price`, `settlement_result`
`days`: `settled_gross`, `total_gross`
`recon`: `settled_gross`

- **Appended, never inserted.** Existing sheet columns must not move: the Dashboard
  and the rollups address `days!H`/`days!I` and `trades!Q`/`R`/`S`/`T`/`U` by letter.
- `gross_basis` ∈ `fills` | `settlement_spot` | `settlement_official` |
  `spot_too_early` | `settlement_file_error` | `""` (an unpriceable execution).
- `days.total_gross` = `gross` + `settled_gross`, blank when any trade that day is
  unvalued — the same "unknown is not zero" rule `gross` already follows.
- `trades.net` = `gross` + `fees` today; it becomes `gross`-or-settled + `fees`, so a
  settlement-valued trade gets a net once the broker reports its OPENING fees (which
  it already does: 2026-09-22 shows `fees -6.88`, the two opening legs).

### Dashboard

- **Net P&L** becomes `SUMIFS(days!<total_gross>) + SUMIFS(days!<argus_fees>)`,
  windowed as every KPI already is.
- **New KPI: "Of which valued at settlement"** = `SUMIFS(days!<settled_gross>)`,
  money format.
- **"Days with an unknown result"** keeps its formula but now counts only genuinely
  unvalued days (`total_gross` blank).
- **`LAST 20 TRADES` gains the basis column**, so a settlement-valued row is visible
  where a person actually looks.
- **Column letters are DERIVED, not counted by hand.** As of this record `days` has
  17 columns (A–Q) and `trades` 27 (A–AA); the build adds to those and **verifies by
  reading the computed cells back** off the real spreadsheet. Four formula defects in
  this project were found only that way, and none of them raised an error
  ([the ledger record](2026-09-22-ledger-and-sheets.md)).

---

## The ITM case, and the one thing left open

An ITM expiry DOES post a broker cash settlement, and its `transaction_type` /
`transaction_sub_type` strings have never been seen — the first live read
(2026-09-22) returned only ordinary `Trade` rows. So:

- **Today's behaviour is kept deliberately**: a broker row with no `order_id` is
  `unmatched_broker` and makes the day `DIFF`. Loud, with the broker's own
  description in `broker_txns`.
- **That will fire on the first ITM expiry**, and it will look like a defect while
  being the designed answer: the settlement-valued trade and the broker's cash row
  are two statements about the same money, and nothing yet knows they are the same.
- **The trigger for closing it, stated so nobody has to re-derive it:** when the
  first such row appears, capture it (account-scrubbed) as a fixture in
  `tests/test_broker_ledger.py`, then teach `reconcile` to pair a no-`order_id`
  settlement row with the settlement-valued trade and compare the two numbers —
  agreeing to the cent is `OK`, disagreeing is `DIFF`. **Do not guess the subtype
  strings in advance**; that is how the `is_estimated_fee` assumption got in, which
  the same live read overturned. **Nor its DATE** (added 2026-09-23, at review): the
  ledger puts a broker row on the ET date of its `executed_at`, and nothing yet says
  a cash settlement is stamped on the expiry's own session rather than that evening
  or the next one. If it lands on D+1, D reads `OK` and D+1 reads `DIFF` for a row
  that belongs to D. Read the captured fixture's `executed_at` and
  `transaction_date` before writing the pairing, and key the pair on the symbol's OCC
  expiry rather than on the row's trading date if the two disagree.

---

## Rails

- **`settlement_liability` and `classify` are the only settlement math.** An AST test
  asserts `tools/settlement.py` contains no `min(`/`max(`/`clamp` arithmetic over
  strikes and imports both from `shadow.outcome`.
- **Never let a settlement-derived number into the broker comparison.** A test
  asserts `recon.argus_gross` equals `days.gross` (fills) and NOT `total_gross`.
- **Unknown stays blank.** No basis, no number: never a zero.
- **The close comes from `MarketCalendar`**, never a literal 16:00 — a half-day
  settles at 13:00 and every date-vs-close comparison here is half-day aware.
- **No new config key**, no migration, no audit category: this is a reader.
- **The ledger still opens no market-data session.** If a future version wants the
  official print automatically, it belongs in the settlements file (written by
  something else), not in a feed read inside the ledger.

---

## Tests the build owes

1. **The real 2026-09-22 day, end to end**: put 7740/7735, 2 contracts, 0.50 credit,
   with a settlement ABOVE the short strike (use `7753.21`, the last recorded spot in
   the 2026-09-21 snapshot — an illustrative value, NOT that day's actual close, which
   the fixture does not need and this record does not claim) → liability 0.0 →
   `full_credit`, gross **+100.00**, fees −6.88, net **+93.12**,
   `gross_basis = settlement_spot`, day `OK`.
2. **`partial` and `max_loss`**: settlement inside the spread (7737.50 → liability
   2.50 → gross −400.00 on 2 contracts) and through it (7730 → liability 5.0 →
   gross −900.00), both against `settlement_liability` directly.
3. **A call spread**, so the side branch is covered rather than assumed.
4. **`spot_too_early`**: newest spot 45 minutes before the close → unvalued,
   `PROVISIONAL`, blank gross.
5. **The override wins** over the recorded spot, and reads `settlement_official`.
6. **A malformed override line** costs its date and nothing else.
7. **A half-day** close is read from the calendar (13:00), not 16:00.
8. **Recon unchanged**: a settlement-valued day still compares fills against the
   broker, `gross_diff` stays `0.00`, and the day reaches `OK`.
9. **An ITM expiry with a broker settlement row** is `DIFF` and names the row.
10. **`days.total_gross` is blank** when any trade that day is unvalued.
11. **The Dashboard's Net P&L includes `settled_gross`** and the new KPI reports it
    (formula-shape test, as with the other report tests).

## Acceptance, on the real spreadsheet

After the next nightly run (the ledger rebuilds every day, so 2026-09-22 re-renders
by itself — no backfill step):

- `trades` row `20260922-put-7740-7735`: gross **100.00** (the spread expired
  worthless — the settlement read from the chain will be well above 7740), net
  **93.12**, `gross_basis settlement_spot`, `settlement_result full_credit`.
- `recon` 2026-09-22: **OK**, `gross_diff 0.00`, `fee_diff 0.00`.
- Dashboard Net P&L: **−$515.20** (−608.32 + 93.12), "Of which valued at settlement"
  **$100.00**, "Days with an unknown result" **0**.
- No error cell on any tab, verified by reading the computed values back.

## What Richard has to do

Nothing, unless an official print ever needs to override a recorded spot — then one
line in `/etc/argus/settlements.csv`:

```
2026-09-22,7753.21
```

The file is optional and absent by default; absent means every expiry is valued from
the recorded spot.

---

## As built (2026-09-23)

`tools/settlement.py` as specified, plus `resolve_settlement` (one date's price or
the reason there is none) and `tools.ledger.load_overrides`. 33 new tests in
`tests/test_settlement.py` cover all eleven owed ones, plus a partly-closed trade,
spot read across `check_spent`, a null spot skipped, another day's spot never read,
the refused-date-does-not-fall-back case, and the column-letter pins.

**The text above cannot all hold at once, and the acceptance numbers decided it.**
On 2026-09-22 the broker reports the two OPENING legs, `value` +100.00, and nothing
for the OTM expiry. `recon` must read `gross_diff 0.00`, and `recon.argus_gross` must
equal `days.gross` — so `days.gross` has to be **+100.00, the cash the fills moved**.
But the KPI must read "Of which valued at settlement" **$100.00** and the Net P&L
**−$515.20**, so `settled_gross` is 100.00 and `total_gross` is 100.00 too. Under
`total_gross = gross + settled_gross` that is 200.00, double-counting the opening
credit. So, as built:

| Column | Is | 2026-09-22 |
|---|---|---|
| `days.gross` | **fill cash** — Σ over the day's fills of signed price × contracts × 100; the recon input, and the same sum the broker's `value` makes. Equals realized P&L on any day where everything closed, so no past day's number moved (re-run against the 2026-09-21 snapshot: 0 / 90 / 100 / −730, unchanged) | 100.00 |
| `days.settled_gross` | Σ gross of trades whose basis is `settlement_spot`/`settlement_official` | 100.00 |
| `days.total_gross` | Σ every trade's gross; blank when any is blank. **The P&L column** | 100.00 |

A consequence worth carrying: **`days.gross` is no longer blank on a held day.** Its
fills are known cash, so the recon now checks the OPENING order to the cent even on
a day whose result is refused (`spot_too_early` reconciles `PROVISIONAL` with
`gross_diff 0.00`), which is a stronger check than the blank it replaces. Every
Dashboard figure that meant "the P&L" — Net P&L, Gross, the `calc` series behind the
charts, Weekly/Monthly `gross` — moved from `days.gross` to `days.total_gross`, and
"Days with an unknown result" counts a blank `total_gross`. `days.net` is
`total_gross` + broker fees.

**`no_spot` is a sixth refusal**, for a date with no spot-bearing mark at all —
distinct from `spot_too_early`, which found one and judged it stale. Different
diagnoses; one basis for both would hide which.

**The settlements file is refused as a WHOLE when it exists but cannot be read**
(permissions, encoding): every settlement valuation that night is
`settlement_file_error` rather than valued from the spot, because no date can be
trusted to be absent from a file nobody could read. The container reads it as uid
10001, so it must be world-readable (it holds no secret). Dates are strictly
`YYYY-MM-DD`; a date listed twice with different prices is refused.

**Column letters are now DERIVED** (`sheets_report.col`, from the row dataclasses'
field order) for every Dashboard, `calc` and rollup reference, not only the new ones.
Every pre-existing letter came out identical; a test pins the ones as of this record
(`days` H/I/Q, `trades` Q/R/S/T/U/AA) and that the new columns are the last fields.
`trades` is now 33 columns (A–AG), `days` 19 (A–S), `recon` 17 (A–Q).

### Review fixes (2026-09-23, same day, before release)

**Comparing CASH with the broker lost the check on the P&L, so a second comparison
now ties them — Argus against its own fills.** Before this build the recon's Argus side
WAS the P&L, so a P&L that disagreed with the broker's cash was a `DIFF`. Once that
side became fill cash, nothing compared the P&L the Dashboard sums with anything:
reproduced with a short leg filled 2 against a long leg filled 1 and a 1×1 close —
`_net_legs` counts one spread and flags nothing, the trade reads gross **30.00**, the
fills moved **150.00**, the broker agrees with the 150.00, and the day reconciled `OK`
(it was a $120 `DIFF` before). The invariant, per trade and to the cent:

```
gross == cash the trade's own fills moved − what its expired contracts owed at settlement
```

It holds exactly for every well-formed vertical (realized `(credit − debit) × closed`
plus settled `(credit − liability) × expired` is `credit × opened − debit × closed −
liability × expired`, and the first two terms are the fills' cash), whichever way the
index finished. A failure fills `trades.pnl_check` with the numbers, names the trade in
`recon.pnl_mismatch`, and makes the day `DIFF` — **even with the broker unread**, since
it needs no broker. It never puts a settlement number in the broker comparison: the
liability is subtracted on Argus's side of an Argus-only identity. `settle_trade` now
returns `SettledTrade(gross, result, liability)` so the liability comes out of the
shared math rather than being re-derived.

Also from that review:

- **`settled_gross` is the EXPIRED contracts' term only**, now a per-trade column too.
  It had summed a partly closed trade's whole gross, counting its fill-realized half
  as "valued at settlement".
- **`trades.settlement_at_et`**: when the recorded spot was taken, on a valued row and
  on a `spot_too_early` refusal (where it is the diagnosis); blank for an official
  print. It is also on the ledger's journal line.
- **`argus-eod.sh` warns on the report channel when the settlements file is not
  readable by uid 10001.** The script runs as root, so `-r` proved nothing, and
  root:600 is how everything else in `/etc/argus` is made.
- The spot query filters a non-positive spot in SQL (so it falls through to the last
  usable one), the file is read as UTF-8 explicitly, and `read_settlement_spots`'
  docstring no longer claims the scan stops early — the plan sorts the day's rows,
  ~20–35 ms on the 2026-09 snapshots.
- The ITM pairing must not assume the broker's row lands on the expiry's own date
  (the ITM section above).

**Not yet done: the acceptance check on the real spreadsheet**, which needs the
deploy, one nightly run, and `tools.sheets report` re-run by hand.
