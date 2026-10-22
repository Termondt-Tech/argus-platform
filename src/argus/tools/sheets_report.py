"""The report tabs: a dashboard, weekly and monthly rollups, and their charts.

    ./.venv/bin/python -m argus.tools.sheets report \\
        --key dev/argus-sheets-key.json --spreadsheet-id <id>

**Built ONCE, by hand, and never by the nightly push.** `tools.sheets push` writes
values into the data tabs and sends no formatting request at all, which is what lets
everything here survive it. Run `report` again only to rebuild.

**Everything is FORMULAS over the data tabs, never numbers copied into a cell.** So
the dashboard is current the moment a push lands, nothing recomputes a P&L a second
way, and a number that looks wrong is traceable to the row it came from. This
project's one-scorer rule applies to spreadsheets too.

**These four tabs are TOOL-OWNED and a rebuild REPLACES them** — `Dashboard`,
`Weekly`, `Monthly` and the hidden `calc`. Put your own work on your own tab; a tab
this module does not name is never touched.

**Four rules learned by building it against the real spreadsheet**, each of which had
produced a plausible-looking wrong number:

- **`SUMIFS` is NOT array-aware.** Wrapped in `ARRAYFORMULA` it returns the FIRST
  row's answer repeated down the column — every week showed week one's P&L. The
  period rollups use `MAP`/`LAMBDA` instead, which evaluates per row. `COUNTIFS`
  happened to work, and is written the same way anyway: a column where one function
  iterates and its neighbour does not is the defect waiting to come back.
- **Two spilling `QUERY` blocks must not share rows.** A result that would overwrite
  a neighbour renders `#REF!` and nothing says why, so every block owns its own rows
  with room to grow.
- **Number formats are per CELL BLOCK, never per column.** A win rate in a money
  column reads `$0.29`; a trade count in one reads `$5.00`.
- **A `QUERY` output needs its own formats.** A date column arrives as a serial
  number (`46286`) unless the region it spills into is formatted as dates.

**Every column letter is DERIVED from the row dataclass that writes the column**
(`col`), never counted by hand. The data tabs grow by appending, and a letter typed
here is the one thing an append cannot move with it — which renders a plausible
number from the wrong column rather than an error.

**The P&L reads `days.total_gross`, never `days.gross`** (2026-09-22). `gross` is the
day's FILL CASH, the reconciliation's input, and on a held-to-expiry day it still
carries the opening credit; `total_gross` is the day's result, settlement-valued
trades included, and blank when any trade is unvalued — which is what "Days with an
unknown result" counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from argus.tools.broker_ledger import ReconRow
from argus.tools.ledger import DayRow, TradeRow
from argus.tools.settlement import VALUED_AT_SETTLEMENT

#: Tool-owned tabs. `calc` is built first: the views reference it.
DASHBOARD, WEEKLY, MONTHLY, CALC = "Dashboard", "Weekly", "Monthly", "calc"
REPORT_TABS = (DASHBOARD, WEEKLY, MONTHLY, CALC)

_INK = {"red": 0.11, "green": 0.13, "blue": 0.16}
_ACCENT = {"red": 0.16, "green": 0.42, "blue": 0.73}
_PAPER = {"red": 0.95, "green": 0.96, "blue": 0.98}
_MUTED = {"red": 0.35, "green": 0.37, "blue": 0.4}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

MONEY = '"$"#,##0.00;[RED]-"$"#,##0.00'
PCT = "0.0%"
DATE = "yyyy-mm-dd"
INT = "#,##0"
PRICE = "0.00"
SECONDS = '#,##0" s"'


@dataclass
class Layout:
    """A tab under construction: rows, plus where the formats and headings go.

    Positions are RECORDED as rows are appended rather than counted by hand — the
    first version hard-coded row numbers and drifted from the content by three rows,
    which is how a heading ended up formatted as a data row.
    """

    title: str
    frozen_rows: int = 1
    hidden: bool = False
    rows: list[list[str]] = field(default_factory=list)
    #: (row0, row1|None, col0, col1, pattern) — half-open, `None` row1 = to the end.
    formats: list[tuple[int, int | None, int, int, str]] = field(default_factory=list)
    headings: list[int] = field(default_factory=list)
    banner: bool = False
    widths: tuple[tuple[int, int, int], ...] = ()

    def add(self, cells: list[str]) -> int:
        self.rows.append(cells)
        return len(self.rows) - 1

    def blanks(self, n: int) -> None:
        for _ in range(n):
            self.add([])

    def heading(self, *cells: str) -> int:
        row = self.add(list(cells))
        self.headings.append(row)
        return row

    def fmt(self, row0: int, row1: int | None, col0: int, col1: int, pattern: str) -> None:
        self.formats.append((row0, row1, col0, col1, pattern))


# -- column addresses, derived ------------------------------------------------


_TAB_ROWS: dict[str, type] = {"days": DayRow, "trades": TradeRow, "recon": ReconRow}


def _letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def col(tab: str, name: str) -> str:
    """The column letter `name` occupies on data tab `tab`, from the row type's own
    field order — the order `ledger.write_csv` writes. A name that is not a field
    raises, so a renamed column fails the build rather than reading a neighbour."""
    names = [f.name for f in fields(_TAB_ROWS[tab])]
    return _letter(names.index(name))


def span(tab: str, name: str, *, absolute: bool = False) -> str:
    """`days!H2:H` — one column below the header. `absolute` pins it (`$H$2:$H`)."""
    c = col(tab, name)
    return f"{tab}!${c}$2:${c}" if absolute else f"{tab}!{c}2:{c}"


def whole(tab: str) -> str:
    """`trades!A2:AD` — every column of the tab, for a QUERY."""
    return f"{tab}!A2:{_letter(len(fields(_TAB_ROWS[tab])) - 1)}"


# -- calc: the chart series ---------------------------------------------------


#: The period dropdown's options. `All` first, so the default shows everything and a
#: filtered dashboard is always a deliberate act.
PERIODS = ("All", "YTD", "This month", "This week", "Last 30 days", "Custom")

#: The resolved window lives on `calc` and EVERY dashboard number reads it from
#: there — one place, so a KPI cannot be left on a different period from its
#: neighbour. `calc!H1` is the dropdown, `H2`/`H3` the resolved dates.
PERIOD_CELL = "Dashboard!$B$3"
CUSTOM_FROM = "Dashboard!$E$3"
CUSTOM_TO = "Dashboard!$F$3"
FROM = "calc!$H$2"
TO = "calc!$H$3"


def window(column: str) -> str:
    """The `,range,">="&from,range,"<="&to` tail every windowed aggregate ends with."""
    return f'{column},">="&{FROM},{column},"<="&{TO}'


def _query_window(column: str) -> str:
    """The same window as a QUERY `where` fragment. QUERY needs date LITERALS, so the
    dates are formatted into the query string rather than compared as cells."""
    return (
        f'" & " and {column} >= date \'" & TEXT({FROM},"yyyy-mm-dd") & "\''
        f' and {column} <= date \'" & TEXT({TO},"yyyy-mm-dd") & "\'"'
    )


def _calc() -> Layout:
    tab = Layout(title=CALC, hidden=True)
    tab.add(["date", "net", "cumulative", "peak", "drawdown", "", "period",
             f'=IFERROR({PERIOD_CELL},"All")'])
    # The series is FILTERED to the window, so the charts and the day-level KPIs
    # (best day, worst day, max drawdown) follow the dropdown without a second
    # mechanism. Cumulative restarts at the window's first session, which is what
    # "show me this month" means.
    dates = span("days", "trading_date")
    in_window = f'{dates}<>"",{dates}>=$H$2,{dates}<=$H$3'
    tab.add([
        f'=IFERROR(FILTER({dates},{in_window}),"")',
        # `N()` on both halves: a BLANK result (a day with a trade nobody could value)
        # contributes zero to the chart rather than erroring the column. The blank
        # itself stays visible on `days`, which is where that fact belongs.
        '=ARRAYFORMULA(IF(LEN(A2:A)=0,"",'
        f'N(FILTER({span("days", "total_gross")},{in_window}))'
        f'+N(FILTER({span("days", "argus_fees")},{in_window}))))',
        '=IFERROR(SCAN(0,FILTER(B2:B,LEN(A2:A)>0),LAMBDA(a,x,a+x)),"")',
        '=IFERROR(SCAN(-1E+15,FILTER(C2:C,LEN(A2:A)>0),LAMBDA(m,c,MAX(m,c))),"")',
        '=ARRAYFORMULA(IF(LEN(A2:A)=0,"",FILTER(C2:C,LEN(A2:A)>0)'
        '-FILTER(D2:D,LEN(A2:A)>0)))',
        "", "from",
        # `from` and `to`, resolved from the dropdown. "Today" is deliberately the
        # LAST SESSION IN THE LEDGER, not the clock: on a Saturday, or before a push,
        # a calendar-based month would silently exclude the newest day it holds.
        '=IFERROR(SWITCH($H$1,'
        '"YTD",DATE(YEAR(MAX(days!A2:A)),1,1),'
        '"This month",EOMONTH(MAX(days!A2:A),-1)+1,'
        '"This week",MAX(days!A2:A)-WEEKDAY(MAX(days!A2:A),3),'
        '"Last 30 days",MAX(days!A2:A)-29,'
        f'"Custom",IF({CUSTOM_FROM}="",MIN(days!A2:A),{CUSTOM_FROM}),'
        'MIN(days!A2:A)),MIN(days!A2:A))',
    ])
    last = "MAX(days!A2:A)"
    tab.add([
        "", "", "", "", "", "", "to",
        f'=IFERROR(IF(AND($H$1="Custom",{CUSTOM_TO}<>""),{CUSTOM_TO},{last}),{last})',
    ])
    tab.fmt(1, None, 0, 1, DATE)
    tab.fmt(1, None, 1, 5, MONEY)
    tab.fmt(1, 4, 7, 8, DATE)
    return tab


# -- the dashboard ------------------------------------------------------------


def _dashboard() -> Layout:
    tab = Layout(title=DASHBOARD, frozen_rows=2, banner=True,
                 widths=((0, 1, 210), (1, 2, 120), (2, 3, 24), (3, 4, 190),
                         (4, 5, 110), (5, 6, 110)))
    tab.add(["Argus — live trading ledger"])
    # The subtitle NAMES THE WINDOW that is showing. A filtered dashboard that looks
    # like an all-time one is the quiet way to misread a number, so the period is on
    # the page rather than only in the dropdown.
    tab.add([f'=IFERROR("Showing "&TEXT({FROM},"d mmm yyyy")&"  →  "'
             f'&TEXT({TO},"d mmm yyyy")&"   ·   "'
             f'&COUNTIFS(days!A2:A,">="&{FROM},days!A2:A,"<="&{TO})&" sessions   ·   "'
             f'&COUNTIFS(trades!A2:A,">="&{FROM},trades!A2:A,"<="&{TO})&" trades",'
             '"no data yet — run tools.ledger, then push")'])

    # The control row. `B3` is the dropdown (data validation is attached in
    # `format_requests`); `E3`/`F3` are read only under "Custom".
    tab.add(["Period", "All", "", "Custom from / to", "", ""])
    tab.fmt(2, 3, 4, 6, DATE)

    days_window = window(span("days", "trading_date"))
    trades_window = window(span("trades", "trading_date"))
    total = span("days", "total_gross")
    settled = span("days", "settled_gross")
    fees = span("days", "argus_fees")
    recon = span("days", "recon")
    gross = span("trades", "gross")
    basis = span("trades", "gross_basis")

    # Two KPI columns. Each row carries its own format, because a rate and a count
    # cannot share one, and EVERY one is windowed — a KPI left unwindowed would sit
    # next to a filtered neighbour and read as the same period.
    tab.heading("RESULTS", "", "", "ACTIVITY", "")
    # A settlement-valued trade COUNTS in the P&L (a winning month would otherwise be
    # understated every time the flatten finds no market, which is its normal
    # outcome) — and is LABELLED: a KPI says how much of the total is not
    # fill-derived, and the trade list names each row's basis.
    settlement_trades = "+".join(
        f'COUNTIFS({basis},"{b}",{trades_window})' for b in VALUED_AT_SETTLEMENT
    )
    kpis: list[tuple[str, str, str, str, str, str]] = [
        ("Net P&L",
         f"=SUMIFS({total},{days_window})+SUMIFS({fees},{days_window})", MONEY,
         "Sessions", f'=COUNTIFS({days_window})', INT),
        ("Gross", f"=SUMIFS({total},{days_window})", MONEY,
         "Days traded",
         f'=COUNTIFS({span("days", "trades")},">0",{days_window})', INT),
        ("Of which valued at settlement", f"=SUMIFS({settled},{days_window})", MONEY,
         "Trades valued at settlement", f"={settlement_trades}", INT),
        ("Fees", f"=SUMIFS({fees},{days_window})", MONEY,
         "Blackout days",
         f'=COUNTIFS({span("days", "blackout")},"yes",{days_window})', INT),
        ("Fees as % of winning gross",
         f'=IFERROR(-SUMIFS({fees},{days_window})'
         f'/SUMIFS({gross},{gross},">0",{trades_window}),"")', PCT,
         "Trades", f'=COUNTIFS({trades_window})', INT),
        ("Best day", '=IFERROR(MAX(calc!B2:B),"")', MONEY,
         "Win rate",
         f'=IFERROR(COUNTIFS({gross},">0",{trades_window})'
         f'/COUNTIFS({gross},"<>",{trades_window}),"")', PCT),
        ("Worst day", '=IFERROR(MIN(calc!B2:B),"")', MONEY,
         "Avg win",
         f'=IFERROR(AVERAGEIFS({gross},{gross},">0",{trades_window}),"")',
         MONEY),
        ("Max drawdown", '=IFERROR(MIN(calc!E2:E),"")', MONEY,
         "Avg loss",
         f'=IFERROR(AVERAGEIFS({gross},{gross},"<0",{trades_window}),"")',
         MONEY),
        ("Avg risk per trade",
         f'=IFERROR(AVERAGEIFS({span("trades", "max_loss")},{trades_window}),"")',
         MONEY,
         "Avg per trade",
         f'=IFERROR(AVERAGEIFS({gross},{trades_window}),"")', MONEY),
        # Blank `total_gross` = a trade valued by NEITHER fills nor settlement.
        ("Days with an unknown result",
         f'=COUNTIFS({total},"",{days_window})', INT,
         "Avg seconds after a stop",
         f'=IFERROR(AVERAGEIFS({span("trades", "seconds_since_prev_exit")},'
         f'{trades_window}),"")', SECONDS),
        # `days.recon` is the day's reconciliation status. "Needs a look" counts
        # everything that is not OK and not blank — a DIFF, an unread broker, or a
        # day still PROVISIONAL — because the operative question is "is there
        # anything here I have not seen?", not "which of the three is it?".
        ("Days reconciled with the broker",
         f'=COUNTIFS({recon},"OK",{days_window})', INT,
         "Days needing a look",
         f'=COUNTIFS({recon},"<>OK",{days_window})'
         f'-COUNTIFS({recon},"",{days_window})', INT),
    ]
    for left, left_formula, left_fmt, right, right_formula, right_fmt in kpis:
        row = tab.add([left, left_formula, "", right, right_formula])
        tab.fmt(row, row + 1, 1, 2, left_fmt)
        tab.fmt(row, row + 1, 4, 5, right_fmt)
    tab.blanks(2)

    # Each QUERY block owns its rows: a spilling result that would overwrite a
    # neighbour renders `#REF!` with nothing saying why.
    def block(heading: str, formula: str, depth: int,
              formats: tuple[tuple[int, int, str], ...]) -> None:
        tab.heading(heading)
        row = tab.add([formula])
        for col0, col1, pattern in formats:
            tab.fmt(row, row + depth, col0, col1, pattern)
        tab.blanks(depth)

    def grouped(source: str, label: str, empty: str) -> str:
        """A grouped QUERY over {date, key, gross}, bounded to the window."""
        return (
            f'=IFERROR(QUERY({{{span("trades", "trading_date")},{source},{gross}}},'
            '"select Col2, count(Col3), sum(Col3), avg(Col3) '
            "where Col2 is not null and Col2 <> ''"
            + _query_window("Col1") +
            ' & " group by Col2 '
            f"label Col2 '{label}', count(Col3) 'trades', sum(Col3) 'gross', "
            f'avg(Col3) \'avg\'",0),"{empty}")'
        )

    block(
        "BY EXIT TRIGGER",
        grouped(span("trades", "exit_trigger"), "trigger", "no exits in this period"),
        6, ((1, 2, INT), (2, 4, MONEY)),
    )
    block(
        "BY SIDE",
        grouped(span("trades", "side"), "side", "—"),
        5, ((1, 2, INT), (2, 4, MONEY)),
    )
    block(
        "BY SHORT-STRIKE SELECTOR  (never pool call outcomes across v0.4.6)",
        grouped(span("trades", "delta_source"), "delta source", "—"),
        5, ((1, 2, INT), (2, 4, MONEY)),
    )
    def select(tab: str, labels: tuple[tuple[str, str], ...]) -> tuple[str, str]:
        """(`A,B,…`, `label A 'date', …`) for QUERY, from field names."""
        letters = [col(tab, name) for name, _ in labels]
        return ",".join(letters), ", ".join(
            f"{c} '{label}'" for c, (_, label) in zip(letters, labels)
        )

    r = lambda name: col("recon", name)  # noqa: E731
    cols, labels = select("recon", (
        ("trading_date", "date"), ("status", "status"), ("gross_diff", "gross diff"),
        ("fee_diff", "fee diff"), ("unmatched_broker", "unmatched at broker"),
        ("estimated_fee_rows", "estimated fees"),
        # A P&L that does not follow from its own fills — DIFF without the broker.
        ("pnl_mismatch", "P&L not from fills"),
    ))
    block(
        "NOT RECONCILED WITH THE BROKER",
        f'=IFERROR(QUERY({whole("recon")},'
        f'"select {cols} where {r("status")} is not null and {r("status")} <> \'OK\''
        + _query_window(r("trading_date"))
        + f' & " order by {r("trading_date")} desc limit 10 label {labels}",0),'
        '"every day reconciles — or the broker has not been read yet")',
        11, ((0, 1, DATE), (2, 4, MONEY)),
    )
    t = lambda name: col("trades", name)  # noqa: E731
    cols, labels = select("trades", (
        ("trading_date", "date"), ("candidate_id", "candidate"), ("side", "side"),
        ("entry_credit", "credit"), ("exit_debit", "debit"), ("gross", "gross"),
        ("gross_pct_of_max_loss", "% of max loss"), ("exit_trigger", "exit trigger"),
        # So a settlement-valued row is visible where a person actually looks.
        ("gross_basis", "basis"),
    ))
    block(
        "LAST 20 TRADES",
        f'=IFERROR(QUERY({whole("trades")},'
        f'"select {cols} where {t("candidate_id")} is not null'
        + _query_window(t("trading_date"))
        + f' & " order by {t("trading_date")} desc, {t("entry_time_et")} desc '
        + f'limit 20 label {labels}",0),"no trades yet")',
        21, ((0, 1, DATE), (3, 5, PRICE), (5, 6, MONEY), (6, 7, "0.0"),),
    )
    return tab


# -- the period rollups -------------------------------------------------------


def _period(title: str, key: str, label: str, next_period: str) -> Layout:
    """Weekly and monthly differ only in the period key and where the next period
    starts, so they are one layout with two arguments.

    **Every aggregate is `MAP`/`LAMBDA`, not `ARRAYFORMULA(SUMIFS(...))`**: `SUMIFS`
    is not array-aware and silently repeats the first row's answer down the column.
    """
    tab = Layout(title=title, widths=((0, 1, 110), (1, 9, 95)))
    tab.add([label, "sessions", "trades", "gross", "fees", "net", "cumulative",
             "win days", "loss days"])

    def agg(expr: str) -> str:
        return (f'=IFERROR(MAP(FILTER(A2:A,LEN(A2:A)>0),LAMBDA(d,{expr})),"")')

    dates = span("days", "trading_date", absolute=True)
    window = f'{dates},">="&d,{dates},"<"&{next_period}'
    tab.add([
        key,
        agg(f"COUNTIFS({window})"),
        agg(f"SUMIFS({span('days', 'trades', absolute=True)},{window})"),
        agg(f"SUMIFS({span('days', 'total_gross', absolute=True)},{window})"),
        agg(f"SUMIFS({span('days', 'argus_fees', absolute=True)},{window})"),
        '=ARRAYFORMULA(IF(LEN(A2:A)=0,"",N(D2:D)+N(E2:E)))',
        '=IFERROR(SCAN(0,FILTER(F2:F,LEN(A2:A)>0),LAMBDA(a,x,a+x)),"")',
        agg(f'COUNTIFS(calc!$A$2:$A,">="&d,calc!$A$2:$A,"<"&{next_period.replace("A2:A", "d")},'
            'calc!$B$2:$B,">0")'),
        agg(f'COUNTIFS(calc!$A$2:$A,">="&d,calc!$A$2:$A,"<"&{next_period.replace("A2:A", "d")},'
            'calc!$B$2:$B,"<0")'),
    ])
    tab.headings.append(0)
    tab.fmt(1, None, 0, 1, DATE)
    tab.fmt(1, None, 1, 3, INT)
    tab.fmt(1, None, 3, 7, MONEY)
    tab.fmt(1, None, 7, 9, INT)
    return tab


def _weekly() -> Layout:
    # WEEKDAY(...,3) counts Monday as 0, so subtracting it lands on that Monday.
    return _period(
        WEEKLY,
        '=IFERROR(SORT(UNIQUE(FILTER(days!A2:A-WEEKDAY(days!A2:A,3),days!A2:A<>""))),"")',
        "week of",
        "d+7",
    )


def _monthly() -> Layout:
    return _period(
        MONTHLY,
        '=IFERROR(SORT(UNIQUE(FILTER(EOMONTH(days!A2:A,-1)+1,days!A2:A<>""))),"")',
        "month",
        "EOMONTH(d,0)+1",
    )


def tabs() -> tuple[Layout, ...]:
    return (_calc(), _dashboard(), _weekly(), _monthly())


# -- formatting ---------------------------------------------------------------


def _range(sheet_id: int, row0: int, row1: int | None, col0: int, col1: int) -> dict:
    out: dict[str, Any] = {"sheetId": sheet_id, "startRowIndex": row0,
                           "startColumnIndex": col0, "endColumnIndex": col1}
    if row1 is not None:
        out["endRowIndex"] = row1
    return out


def format_requests(tab: Layout, sheet_id: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "hidden": tab.hidden,
                "gridProperties": {"frozenRowCount": tab.frozen_rows},
            },
            "fields": "hidden,gridProperties.frozenRowCount",
        }
    }]
    if tab.banner:
        out += [
            {"mergeCells": {"range": _range(sheet_id, 0, 1, 0, 6),
                            "mergeType": "MERGE_ROWS"}},
            {"repeatCell": {
                "range": _range(sheet_id, 0, 1, 0, 6),
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _INK,
                    "textFormat": {"foregroundColor": _WHITE, "bold": True,
                                   "fontSize": 15},
                    "verticalAlignment": "MIDDLE",
                    "padding": {"left": 12},
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,"
                          "verticalAlignment,padding)",
            }},
            {"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 42}, "fields": "pixelSize",
            }},
            {"repeatCell": {
                "range": _range(sheet_id, 1, 2, 0, 6),
                "cell": {"userEnteredFormat": {
                    "textFormat": {"italic": True, "foregroundColor": _MUTED},
                }},
                "fields": "userEnteredFormat.textFormat",
            }},
        ]
    if tab.title == DASHBOARD:
        # The period dropdown. `strict` so a typo cannot silently resolve to `All`,
        # and the list is short on purpose: a filter nobody understands is worse than
        # no filter.
        out.append({"setDataValidation": {
            "range": _range(sheet_id, 2, 3, 1, 2),
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": p} for p in PERIODS]},
                "showCustomUi": True,
                "strict": True,
                "inputMessage": "Which period the dashboard shows",
            },
        }})
        out.append({"repeatCell": {
            "range": _range(sheet_id, 2, 3, 0, 6),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _PAPER,
                "textFormat": {"bold": True, "foregroundColor": _MUTED},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})
        out.append({"repeatCell": {
            "range": _range(sheet_id, 2, 3, 1, 2),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _WHITE,
                "textFormat": {"bold": True, "foregroundColor": _ACCENT},
                "borders": {side: {"style": "SOLID", "colorStyle":
                                   {"rgbColor": _ACCENT}}
                            for side in ("top", "bottom", "left", "right")},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,borders)",
        }})

    for row in tab.headings:
        dark = not tab.banner  # a data-style tab gets the solid header
        out.append({"repeatCell": {
            "range": _range(sheet_id, row, row + 1, 0, 9),
            "cell": {"userEnteredFormat": {
                "backgroundColor": _ACCENT if dark else _PAPER,
                "textFormat": {"bold": True,
                               "foregroundColor": _WHITE if dark else _ACCENT},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        }})
    for row0, row1, col0, col1, pattern in tab.formats:
        out.append({"repeatCell": {
            "range": _range(sheet_id, row0, row1, col0, col1),
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "NUMBER", "pattern": pattern}
            }},
            "fields": "userEnteredFormat.numberFormat",
        }})
    for start, end, pixels in tab.widths:
        out.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": pixels}, "fields": "pixelSize",
        }})
    return out


# -- charts -------------------------------------------------------------------


def _series(sheet_id: int, column: int, rows: int = 2000) -> dict[str, Any]:
    return {"sourceRange": {"sources": [{
        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": rows,
        "startColumnIndex": column, "endColumnIndex": column + 1,
    }]}}


def chart_requests(ids: dict[str, int]) -> list[dict[str, Any]]:
    """Three charts down the right-hand side of the Dashboard.

    A chart is an object on the sheet rather than a cell, so a value push never
    disturbs one — but a REBUILD recreates the tab, which is why `report` replaces
    the tab wholesale instead of reconciling charts it placed before.
    """
    def chart(title: str, kind: str, source: int, value_col: int,
              anchor_row: int) -> dict[str, Any]:
        return {"addChart": {"chart": {
            "spec": {
                "title": title,
                "titleTextFormat": {"bold": True, "fontSize": 12},
                "basicChart": {
                    "chartType": kind,
                    "legendPosition": "NO_LEGEND",
                    "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
                    "domains": [{"domain": _series(source, 0)}],
                    "series": [{
                        "series": _series(source, value_col),
                        "targetAxis": "LEFT_AXIS",
                        "colorStyle": {"rgbColor": _ACCENT},
                    }],
                    "headerCount": 0,
                },
            },
            "position": {"overlayPosition": {
                "anchorCell": {"sheetId": ids[DASHBOARD], "rowIndex": anchor_row,
                               "columnIndex": 6},
                "widthPixels": 600, "heightPixels": 250,
            }},
        }}}

    return [
        chart("Cumulative net P&L", "LINE", ids[CALC], 2, 2),
        chart("Net P&L by day", "COLUMN", ids[CALC], 1, 15),
        chart("Net P&L by week", "COLUMN", ids[WEEKLY], 5, 28),
    ]
