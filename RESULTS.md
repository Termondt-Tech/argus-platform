# Results: the example strategy's verdict

The strategy in `src/argus/signal/` sells 0DTE SPX credit spreads. It was traded live at
minimum size from 2026-09-15 to 2026-10-01, and replayed on six years of history before any
size increase was considered. The history said no, and the day after it reported, the
owner engaged the kill switch. This page states the verdict in per-session terms; part two
of the accompanying articles tells the story.

## What was tested

**The book**, as configured in `argus.live.toml` at the time: two slots, one per side, one
contract a spread, a $475 maximum loss a spread and $1,050 a day; the short strike at 0.15
mid-implied delta, 5 points wide, at least $0.50 of credit, entries from 10:00 to 15:00 ET;
the strike-touch stop; the 15:30 close-out with its far-out-of-the-money deferral.

**The data**: vendor SPXW 0DTE quotes, the last NBBO at each minute mark, every strike.
A history set from 2020-01-02 to 2025-09-30 (1,171 scored sessions) and a held-out period
from 2025-10-01 to 2026-07-17 (191 sessions), scored once, with the questions, the scoring
and what each answer would allow fixed in writing before the held-out period was opened.

## What it found

| | History set | Held-out period |
|---|---|---|
| Net per session, every fill at the mid | −$2.29 | −$13.14 |
| Net per session, at the fill quality the live orders showed | about −$19 | about −$24 |

- **Why it is flat.** One trade in five is stopped out, and a stop costs about what four
  winning trades earn. Before fees the book makes about $6.65 a session; fees are about $9.
- **Its design choices are right.** The strike-touch stop beats no stop and the earlier
  credit-multiple stop; the late-day deferral beats always closing; afternoon premium is
  not richer.
- **The readings are real.** Nine predictions written before the held-out period was opened
  all came true; luck gives eight or more about 2% of the time.
- **Nothing tested pays after costs.** Every variant that did better traded less or paid
  less friction. They are less bad, not profitable.

## What this means for a reader

- The platform's rails did their job: the strategy was stopped on evidence, by the
  kill switch the constitution requires, and its record is intact. The rails are what
  this repository is for.
- A backtest filled at the mid flatters a 0DTE seller; the second row of the table is the
  one to believe, and the live fills are what it was calibrated on.
- The research record itself (the pre-registration, the scripts, the vendor data under a
  personal-use licence) is private and not in this tree.
