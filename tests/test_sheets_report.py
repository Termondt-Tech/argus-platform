"""The report tabs (`argus.tools.sheets_report`).

**Every test here is a defect the real spreadsheet produced**, each of which rendered
a plausible number rather than an error:

- `ARRAYFORMULA(SUMIFS(...))` repeated week one's P&L down the whole column;
- `QUERY(..., 1)` over a range starting at row 2 ate the first TRADE as a header,
  so 09-17 vanished from every grouping and the day count silently disagreed;
- two spilling `QUERY` blocks sharing rows rendered `#REF!`;
- a whole-column money format showed a win rate as `$0.29` and a count as `$5.00`.

A spreadsheet cannot be unit-tested end to end from here, so these pin the shape of
what is SENT; the build was also verified against the live spreadsheet by reading the
computed values back.
"""

from __future__ import annotations

import json
import re

import pytest

from argus.tools import sheets, sheets_report
from argus.tools.sheets_report import DASHBOARD, REPORT_TABS, tabs

from test_sheets import (  # noqa: F401  (key_file is used as a fixture)
    FakeGoogle,
    key_file,
    needs_openssl,
)


def _formulas() -> list[tuple[str, str]]:
    return [
        (tab.title, cell)
        for tab in tabs()
        for row in tab.rows
        for cell in row
        if cell.startswith("=")
    ]


# -- the four formula defects -------------------------------------------------


def test_no_aggregate_is_an_arrayformula_over_sumifs():
    """`SUMIFS` is not array-aware: inside `ARRAYFORMULA` it returns the first row's
    answer repeated down the column. The rollups use MAP/LAMBDA."""
    for title, formula in _formulas():
        if "SUMIFS" not in formula:
            continue
        assert "ARRAYFORMULA" not in formula, f"{title}: {formula[:60]}"
        if title in ("Weekly", "Monthly"):
            # These spill a column, one row per period, which is the context where
            # SUMIFS quietly stops iterating. A single-cell SUMIFS is fine.
            assert "MAP(" in formula, f"{title}: a spilled SUMIFS must be mapped"


def test_every_query_is_told_it_has_no_header_row():
    """The data ranges start at row 2, so `headers=1` consumes the first real row."""
    for title, formula in _formulas():
        for call in re.findall(r'",\s*(\d)\)', formula):
            assert call == "0", f"{title}: QUERY headers must be 0, got {call}"
        if "QUERY(" in formula:
            assert '",0)' in formula, f"{title}: QUERY without an explicit headers=0"


def test_query_blocks_never_share_rows_with_the_next_block():
    """A spilling result that would overwrite a neighbour renders `#REF!`."""
    for tab in tabs():
        for i, row in enumerate(tab.rows):
            if not any("QUERY(" in cell for cell in row):
                continue
            assert len(row) == 1, f"{tab.title} row {i}: a QUERY shares its row"
            following = tab.rows[i + 1:i + 5]
            assert all(not any(c.strip() for c in r) for r in following), \
                f"{tab.title} row {i}: fewer than 4 free rows below a QUERY"


def test_kpi_number_formats_are_bounded_to_their_own_cell():
    """A rate and a count cannot share a column format."""
    dashboard = next(t for t in tabs() if t.title == DASHBOARD)
    kpi_formats = [f for f in dashboard.formats if f[1] is not None and f[3] - f[2] == 1]
    assert kpi_formats, "the KPI rows must carry per-cell formats"
    for row0, row1, _, _, _ in kpi_formats:
        assert row1 == row0 + 1 or row1 > row0, "a format block must not be open-ended"
    patterns = {f[4] for f in kpi_formats}
    assert sheets_report.PCT in patterns and sheets_report.INT in patterns, \
        "rates and counts must have their own patterns"


def test_a_query_output_region_is_formatted_where_it_lands():
    """A date column arrives as a serial number (46286) unless its region is
    formatted as dates."""
    dashboard = next(t for t in tabs() if t.title == DASHBOARD)
    query_rows = [
        i for i, row in enumerate(dashboard.rows)
        if any("QUERY(trades!A2:" in c for c in row)
    ]
    assert query_rows, "the trade list must exist"
    row = query_rows[0]
    covering = [
        f for f in dashboard.formats
        if f[0] == row and f[4] == sheets_report.DATE and f[2] == 0
    ]
    assert covering, "the trade list's date column has no date format"
    assert covering[0][1] > row + 1, "the format must cover the rows it spills into"


# -- the period filter --------------------------------------------------------


def test_every_dashboard_number_reads_the_same_window():
    """A KPI left unwindowed sits beside filtered neighbours and reads as the same
    period. The window is one pair of cells on `calc`, and everything reads it —
    directly, or through the `calc` series, which is filtered to it."""
    dashboard = next(t for t in tabs() if t.title == DASHBOARD)
    aggregates = [
        c for row in dashboard.rows for c in row
        if c.startswith("=") and any(
            fn in c for fn in ("SUMIFS", "COUNTIFS", "AVERAGEIFS", "QUERY")
        )
    ]
    assert aggregates
    for formula in aggregates:
        assert sheets_report.FROM in formula and sheets_report.TO in formula, formula[:70]

    windowless = [
        c for row in dashboard.rows for c in row
        if c.startswith("=") and re.search(r"\b(SUM|COUNT|COUNTA|AVERAGE)\(", c)
    ]
    assert not windowless, "an unwindowed whole-column aggregate ignores the filter"


def test_the_subtitle_names_the_window_that_is_showing():
    """A filtered dashboard that looks like an all-time one is the quiet misread."""
    dashboard = next(t for t in tabs() if t.title == DASHBOARD)
    subtitle = dashboard.rows[1][0]
    assert "Showing" in subtitle
    assert sheets_report.FROM in subtitle and sheets_report.TO in subtitle


def test_the_period_cell_offers_exactly_the_documented_options():
    requests = sheets_report.format_requests(
        next(t for t in tabs() if t.title == DASHBOARD), 7
    )
    [validation] = [r["setDataValidation"] for r in requests if "setDataValidation" in r]

    values = [v["userEnteredValue"] for v in validation["rule"]["condition"]["values"]]
    assert values == list(sheets_report.PERIODS)
    assert validation["rule"]["strict"], "a typo must not silently resolve to All"
    assert validation["range"]["startRowIndex"] == 2, "the control row"


def test_the_window_falls_back_to_the_whole_ledger():
    """Every branch that could be blank — an unknown period, an empty custom date —
    resolves to the full range rather than to an error or an empty dashboard."""
    calc = next(t for t in tabs() if t.title == "calc")
    from_cell = calc.rows[1][7]
    to_cell = calc.rows[2][7]

    assert from_cell.count("MIN(days!A2:A)") >= 2, "default and custom-blank both fall back"
    assert from_cell.startswith("=IFERROR(") and to_cell.startswith("=IFERROR(")
    assert "MAX(days!A2:A)" in to_cell


def test_the_window_is_anchored_on_the_last_session_not_the_clock():
    """On a Saturday, or before a push, a clock-based month would exclude the newest
    day the ledger holds."""
    calc = next(t for t in tabs() if t.title == "calc")
    windowed = " ".join(calc.rows[1] + calc.rows[2])
    assert "TODAY()" not in windowed and "NOW()" not in windowed
    assert "MAX(days!A2:A)" in windowed


def test_the_chart_series_is_filtered_to_the_window():
    calc = next(t for t in tabs() if t.title == "calc")
    assert "$H$2" in calc.rows[1][0] and "$H$3" in calc.rows[1][0]
    assert "$H$2" in calc.rows[1][1] and "$H$3" in calc.rows[1][1]


# -- the layout ---------------------------------------------------------------


def test_the_dashboard_reads_the_data_tabs_and_never_copies_a_number():
    """Formulas, so the dashboard is current the moment a push lands and nothing
    scores the P&L a second way."""
    dashboard = next(t for t in tabs() if t.title == DASHBOARD)
    cells = [c for row in dashboard.rows for c in row if c]

    def is_number(cell: str) -> bool:
        try:
            float(cell.replace(",", "").replace("$", ""))
        except ValueError:
            return False
        return True

    assert not [c for c in cells if is_number(c)], \
        "a number typed into the dashboard would be a second scorer"
    joined = " ".join(cells)
    assert "days!" in joined and "trades!" in joined and "calc!" in joined


def test_the_calc_tab_is_hidden_and_the_views_are_not():
    by_title = {t.title: t for t in tabs()}
    assert by_title["calc"].hidden
    assert not any(by_title[t].hidden for t in ("Dashboard", "Weekly", "Monthly"))


def test_a_blank_gross_contributes_zero_to_the_chart_series_not_an_error():
    calc = next(t for t in tabs() if t.title == "calc")
    net = calc.rows[1][1]
    # `(?<![A-Z])N\(` so `LEN(` does not count as an `N(`.
    assert len(re.findall(r"(?<![A-Z])N\(", net)) == 2, \
        "both halves wrapped, so a day with an unknown result is zero here"


def test_the_charts_live_on_the_dashboard_and_read_the_series_tabs():
    ids = {t: i for i, t in enumerate(REPORT_TABS)}
    charts = sheets_report.chart_requests(ids)

    assert len(charts) == 3
    for chart in charts:
        spec = chart["addChart"]["chart"]
        anchor = spec["position"]["overlayPosition"]["anchorCell"]
        assert anchor["sheetId"] == ids[DASHBOARD], "charts belong on the dashboard"
        source = spec["spec"]["basicChart"]["domains"][0]["domain"]
        sheet_id = source["sourceRange"]["sources"][0]["sheetId"]
        assert sheet_id in (ids["calc"], ids["Weekly"]), "series come from the rollups"


# -- rebuilding ---------------------------------------------------------------


@needs_openssl
def test_a_rebuild_deletes_only_the_tool_owned_tabs(key_file):
    google = FakeGoogle(key_file, tabs=("days", "trades", "my report", *REPORT_TABS))

    sheets.build_report(key_file, "sheet-1", google)

    deleted = [w["deleteSheet"]["sheetId"] for w in google.writes if "deleteSheet" in w]
    by_id = {i: t for i, t in enumerate(("days", "trades", "my report", *REPORT_TABS))}
    assert {by_id[i] for i in deleted} == set(REPORT_TABS), \
        "a tab this tool does not own is never deleted"


@needs_openssl
def test_a_rebuild_writes_the_formulas_as_formulas(key_file):
    google = FakeGoogle(key_file, tabs=("days", "trades"))

    sheets.build_report(key_file, "sheet-1", google)

    writes = [r for r in google.requests if r.get_method() == "PUT"]
    assert {u.full_url.count("USER_ENTERED") for u in writes} == {1}, \
        "RAW would put the formulas in as text"
    written = {json.dumps(json.loads(r.data)["values"]) for r in writes}
    assert any("QUERY" in w for w in written)


@needs_openssl
def test_a_rebuild_sends_no_value_write_to_a_data_tab(key_file):
    """The report never touches the data tabs; the push owns those."""
    google = FakeGoogle(key_file, tabs=("days", "trades"))

    sheets.build_report(key_file, "sheet-1", google)

    for req in google.requests:
        if req.get_method() == "PUT":
            assert not any(f"'{t}'" in req.full_url for t in sheets.DATA_TABS)


@needs_openssl
def test_a_spreadsheet_that_does_not_report_its_new_tabs_is_a_failure(key_file):
    class Silent(FakeGoogle):
        def __call__(self, req):
            out = super().__call__(req)
            return {} if req.full_url.endswith(":batchUpdate") else out

    with pytest.raises(sheets.SheetsError, match="did not report new tab"):
        sheets.build_report(key_file, "sheet-1", Silent(key_file, tabs=("days",)))
