"""Google Sheets for the trading ledger: the service-account login and the calls.

    ./.venv/bin/python -m argus.tools.sheets check \\
        --key dev/argus-sheets-key.json --spreadsheet-id <id>

`check` proves the credential works before anything is deployed: it signs in,
confirms the spreadsheet is reachable and writable, and creates the data tabs.

**A SERVICE ACCOUNT, and the spreadsheet is shared with it.** No consent screen, no
publishing status, no domain and no 7-day token expiry — an OAuth app cannot be
published for personal use without a domain, a privacy policy and Google's brand
verification. The service account holds NO project role, so the key reaches nothing
in Google Cloud; its only access is the one spreadsheet shared with its
`client_email`. → docs/decisions/2026-09-22-ledger-and-sheets.md

**This is the one `argus.tools.*` entry point without `--config`**: that rule exists
because a CWD-relative `db_path` once opened the live database, and this module
imports no `argus` runtime, no `sqlite3` and no `tastytrade` code at all (an AST test
pins it), so it has no path to a database or to anything that trades.

**Standard library only.** The JWT is signed by `tools.rs256`, which is tested
against `openssl`. Everything else is `urllib`.

**The key file is a secret.** Never in `.env` (the trading container's `env_file`),
never in git, and on the droplet owned by the container's uid (10001) at mode 400.
Nothing here prints or logs a key or a token.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from argus.tools import sheets_report
from argus.tools.rs256 import KeyError_, RsaKey, b64url, load_pem_private_key, sign

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: The machine-owned tabs, replaced in full on every push. `days` first because it is
#: the one a person opens. Report tabs are added by hand and never touched.
DATA_TABS = ("days", "trades", "argus_fills", "broker_txns", "recon")

#: A Google assertion may claim at most an hour.
TOKEN_LIFETIME_S = 3600
HTTP_TIMEOUT_S = 30.0


class SheetsError(RuntimeError):
    """A Google call failed. The message carries Google's own error text, never a
    key or a token."""


# -- the key file -------------------------------------------------------------


@dataclass(frozen=True)
class ServiceAccount:
    client_email: str
    private_key: RsaKey
    project_id: str = ""

    def __str__(self) -> str:  # never render the key
        return f"service account {self.client_email}"


def load_key(path: Path) -> ServiceAccount:
    """The service account from the JSON Google Cloud downloads once.

    An OAUTH client file is refused by name: it has `installed`/`web` instead of
    `private_key`, and telling the two apart here is cheaper than a confusing
    failure at Google's token endpoint.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SheetsError(f"cannot read the key file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SheetsError(f"{path} is not a JSON object")
    if "private_key" not in raw:
        kind = "OAuth client" if raw.keys() & {"installed", "web"} else "unknown"
        raise SheetsError(
            f"{path} is a {kind} file, not a service-account key. Create a service "
            "account, then Keys -> Add key -> Create new key -> JSON."
        )
    if raw.get("type") not in (None, "service_account"):
        raise SheetsError(f"{path} has type {raw['type']!r}, expected service_account")
    try:
        key = load_pem_private_key(str(raw["private_key"]))
    except KeyError_ as exc:
        raise SheetsError(f"{path} has an unreadable private_key: {exc}") from exc
    email = str(raw.get("client_email") or "")
    if not email:
        raise SheetsError(f"{path} has no client_email")
    return ServiceAccount(client_email=email, private_key=key,
                          project_id=str(raw.get("project_id") or ""))


# -- HTTP ---------------------------------------------------------------------

#: `(request) -> parsed JSON`. Injected so tests never reach Google.
Transport = Callable[[urllib.request.Request], dict[str, Any]]


def urllib_transport(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise SheetsError(
            f"{request.get_method()} {request.full_url.split('?')[0]} -> "
            f"HTTP {exc.code}: {_google_error(exc.read())}"
        ) from None
    except urllib.error.URLError as exc:
        raise SheetsError(
            f"{request.get_method()} {request.full_url.split('?')[0]} failed: {exc.reason}"
        ) from None
    return json.loads(body) if body else {}


def _google_error(body: bytes) -> str:
    """Google's own error text, or the raw body if it is not the usual shape."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body.decode(errors="replace")[:300]
    err = parsed.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(parsed.get("error_description") or err or parsed)[:300]


def _api(method: str, url: str, token: str, body: Any = None) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )


# -- the login ----------------------------------------------------------------


def build_assertion(account: ServiceAccount, now: int | None = None) -> str:
    """The signed JWT Google trades for an access token."""
    issued = int(time.time()) if now is None else now
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": account.client_email,
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": issued,
        "exp": issued + TOKEN_LIFETIME_S,
    }
    def part(obj: dict[str, Any]) -> str:
        return b64url(json.dumps(obj, separators=(",", ":")).encode())
    signing_input = f"{part(header)}.{part(claims)}".encode()
    return f"{signing_input.decode()}.{b64url(sign(signing_input, account.private_key))}"


def access_token(
    account: ServiceAccount, transport: Transport = urllib_transport
) -> str:
    """A one-hour access token. What every push starts with."""
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode({
            "grant_type": JWT_BEARER,
            "assertion": build_assertion(account),
        }).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    token = transport(request).get("access_token")
    if not token:
        raise SheetsError("the token endpoint returned no access_token")
    return str(token)


# -- the spreadsheet ----------------------------------------------------------


@dataclass(frozen=True)
class Spreadsheet:
    id: str
    title: str
    url: str
    tabs: tuple[str, ...]
    created_tabs: tuple[str, ...] = ()


def _get(spreadsheet_id: str, token: str, transport: Transport) -> dict[str, Any]:
    fields = (
        "spreadsheetUrl,properties.title,"
        "sheets.properties(title,sheetId,gridProperties(rowCount,columnCount))"
    )
    return transport(_api(
        "GET",
        f"{SHEETS_URL}/{urllib.parse.quote(spreadsheet_id)}?fields={fields}",
        token,
    ))


def _grid(doc: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """tab title -> (sheet id, rows, columns) it currently HAS.

    The ID is load-bearing: `updateSheetProperties` identifies a sheet ONLY by
    `sheetId`, and a request that names the title instead silently addresses sheet
    0 — which is some other tab, or none at all once that tab is deleted. The grid
    bound matters because a value write past it is refused with "exceeds grid
    limits"; sheets do not grow on their own, so `push` grows them first.
    """
    out: dict[str, tuple[int, int, int]] = {}
    for sheet in doc.get("sheets", []):
        props = sheet.get("properties") or {}
        title = props.get("title")
        if not title:
            continue
        grid = props.get("gridProperties") or {}
        out[str(title)] = (
            int(props.get("sheetId", -1)),
            int(grid.get("rowCount") or 0),
            int(grid.get("columnCount") or 0),
        )
    return out


def ensure_data_tabs(
    spreadsheet_id: str, token: str, transport: Transport
) -> Spreadsheet:
    """Make sure every data tab exists. Never deletes or renames anything: the report
    tabs are the operator's, and this tool has no business touching them.

    Creating a tab is also the WRITE test — it is refused unless the service account
    has Editor on the file. When nothing is missing, a no-op property update stands
    in for it, so `check` can never report success on a read-only share.
    """
    doc = _get(spreadsheet_id, token, transport)
    title = str((doc.get("properties") or {}).get("title") or "")
    have = tuple(
        str(s["properties"]["title"]) for s in doc.get("sheets", [])
        if (s.get("properties") or {}).get("title")
    )
    missing = tuple(t for t in DATA_TABS if t not in have)
    requests: list[dict[str, Any]] = [
        {"addSheet": {"properties": {"title": t}}} for t in missing
    ]
    if not requests:
        # Sets the title to what it already is: a write that changes nothing.
        requests = [{"updateSpreadsheetProperties": {
            "properties": {"title": title}, "fields": "title",
        }}]
    transport(_api(
        "POST", f"{SHEETS_URL}/{urllib.parse.quote(spreadsheet_id)}:batchUpdate",
        token, {"requests": requests},
    ))
    return Spreadsheet(
        id=spreadsheet_id,
        title=title,
        url=str(doc.get("spreadsheetUrl")
                or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"),
        tabs=tuple(have) + missing,
        created_tabs=missing,
    )


def check(
    key_path: Path, spreadsheet_id: str, transport: Transport = urllib_transport
) -> tuple[ServiceAccount, Spreadsheet]:
    account = load_key(key_path)
    token = access_token(account, transport)
    return account, ensure_data_tabs(spreadsheet_id, token, transport)


# -- the push -----------------------------------------------------------------

#: A leading one of these makes Sheets treat a cell as a formula under
#: `USER_ENTERED`. Nothing the ledger writes starts with one, and `push` refuses
#: rather than sending it: a value silently becoming a formula is the quiet kind of
#: wrong this whole project keeps designing against. A leading `-` is fine — that is
#: a negative number, which is most of what a losing day contains.
_FORMULA_STARTS = ("=", "+", "@")


@dataclass(frozen=True)
class TabPush:
    tab: str
    rows: int
    columns: int
    grew_grid: bool


def read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(newline="") as fh:
        return [row for row in csv.reader(fh)]


def _refuse_formulas(tab: str, rows: list[list[str]]) -> None:
    for r, row in enumerate(rows, start=1):
        for c, cell in enumerate(row, start=1):
            if cell.startswith(_FORMULA_STARTS):
                raise SheetsError(
                    f"{tab} row {r} column {c} starts with {cell[:1]!r}, which Sheets "
                    f"would read as a formula: {cell[:40]!r}"
                )


def push_tab(
    spreadsheet_id: str,
    tab: str,
    rows: list[list[str]],
    token: str,
    transport: Transport,
    grid: tuple[int, int, int] = (-1, 0, 0),
) -> TabPush:
    """Replace one tab's VALUES. Formatting is untouched, by construction.

    `values.clear` and `values.update` change values only — number formats, fonts,
    widths and frozen rows are properties of the cells and are changed only through
    `spreadsheets.batchUpdate`. So whatever the operator formats by hand survives
    every push, which is why this tool deliberately formats nothing.
    """
    _refuse_formulas(tab, rows)
    needed_rows = max(len(rows), 1)
    needed_cols = max((len(r) for r in rows), default=1)
    sheet_id, have_rows, have_cols = grid
    grew = needed_rows > have_rows or needed_cols > have_cols
    quoted = urllib.parse.quote(f"'{tab}'")
    if grew and sheet_id < 0:
        raise SheetsError(
            f"{tab} needs a bigger grid but the spreadsheet reported no sheetId for "
            "it; refusing to send a resize that would address another tab"
        )
    if grew:
        transport(_api(
            "POST", f"{SHEETS_URL}/{urllib.parse.quote(spreadsheet_id)}:batchUpdate",
            token,
            {"requests": [{"updateSheetProperties": {
                "properties": {
                    # BY ID. A title here is ignored and the request lands on sheet 0.
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "rowCount": max(needed_rows, have_rows),
                        "columnCount": max(needed_cols, have_cols),
                    },
                },
                "fields": "gridProperties(rowCount,columnCount)",
            }}]},
        ))
    # Clear first: a shorter ledger than last night must not leave stale rows below
    # the new data. Values only — the formats stay.
    transport(_api(
        "POST",
        f"{SHEETS_URL}/{urllib.parse.quote(spreadsheet_id)}/values/{quoted}:clear",
        token, {},
    ))
    transport(_api(
        "PUT",
        f"{SHEETS_URL}/{urllib.parse.quote(spreadsheet_id)}/values/{quoted}!A1"
        "?valueInputOption=USER_ENTERED",
        token, {"values": rows},
    ))
    return TabPush(tab=tab, rows=len(rows), columns=needed_cols, grew_grid=grew)


def push(
    key_path: Path,
    spreadsheet_id: str,
    csv_dir: Path,
    transport: Transport = urllib_transport,
) -> tuple[Spreadsheet, tuple[TabPush, ...]]:
    """Replace every data tab that has a CSV in `csv_dir`.

    A tab whose CSV is absent is LEFT ALONE rather than emptied: before the broker
    step exists there is no `broker_txns.csv`, and an empty tab would read as "the
    broker reported nothing" instead of "this is not built yet".
    """
    account = load_key(key_path)
    token = access_token(account, transport)
    sheet = ensure_data_tabs(spreadsheet_id, token, transport)
    grid = _grid(_get(spreadsheet_id, token, transport))

    pushes: list[TabPush] = []
    for tab in DATA_TABS:
        path = csv_dir / f"{tab}.csv"
        if not path.is_file():
            continue
        rows = read_csv_rows(path)
        if not rows:
            raise SheetsError(f"{path} is empty — refusing to blank {tab}")
        pushes.append(push_tab(
            spreadsheet_id, tab, rows, token, transport, grid.get(tab, (-1, 0, 0))
        ))
    if not pushes:
        raise SheetsError(f"no data-tab CSVs found in {csv_dir}")
    return sheet, tuple(pushes)


# -- the report tabs ----------------------------------------------------------


def _batch(
    spreadsheet_id: str, token: str, transport: Transport, requests: list[dict[str, Any]]
) -> dict[str, Any]:
    return transport(_api(
        "POST", f"{SHEETS_URL}/{urllib.parse.quote(spreadsheet_id)}:batchUpdate",
        token, {"requests": requests},
    ))


def build_report(
    key_path: Path, spreadsheet_id: str, transport: Transport = urllib_transport
) -> tuple[Spreadsheet, tuple[str, ...]]:
    """(Re)build the tool-owned report tabs: `Dashboard`, `Weekly`, `Monthly`, `calc`.

    A rebuild DELETES and recreates exactly those four and nothing else — a tab this
    tool does not name is never touched, so the operator's own work is safe. The tabs
    hold formulas over the data tabs, so they are current the moment a push lands and
    this command never has to run again.
    """
    account = load_key(key_path)
    token = access_token(account, transport)
    sheet = ensure_data_tabs(spreadsheet_id, token, transport)

    doc = _get(spreadsheet_id, token, transport)
    existing = {
        str((s.get("properties") or {}).get("title")):
            int((s.get("properties") or {}).get("sheetId", -1))
        for s in doc.get("sheets", [])
    }
    layout = sheets_report.tabs()
    # Delete first, then add: names are unique, so a rebuild cannot collide with the
    # tab it replaces. Only ever the four this tool owns.
    requests: list[dict[str, Any]] = [
        {"deleteSheet": {"sheetId": existing[t]}}
        for t in sheets_report.REPORT_TABS if t in existing and existing[t] >= 0
    ]
    requests += [{"addSheet": {"properties": {"title": t.title}}} for t in layout]
    reply = _batch(spreadsheet_id, token, transport, requests)
    ids: dict[str, int] = {}
    for one in reply.get("replies", []):
        props = (one.get("addSheet") or {}).get("properties") or {}
        if props.get("title"):
            ids[str(props["title"])] = int(props["sheetId"])
    missing = [t.title for t in layout if t.title not in ids]
    if missing:
        raise SheetsError(f"the spreadsheet did not report new tab(s): {missing}")

    for tab in layout:
        transport(_api(
            "PUT",
            f"{SHEETS_URL}/{urllib.parse.quote(spreadsheet_id)}/values/"
            f"{urllib.parse.quote(chr(39) + tab.title + chr(39))}!A1"
            "?valueInputOption=USER_ENTERED",
            token, {"values": tab.rows},
        ))

    dress: list[dict[str, Any]] = []
    for tab in layout:
        dress += sheets_report.format_requests(tab, ids[tab.title])
    dress += sheets_report.chart_requests(ids)
    _batch(spreadsheet_id, token, transport, dress)
    return sheet, tuple(t.title for t in layout)


# -- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    chk = sub.add_parser(
        "check", help="prove the key and the spreadsheet share work; create data tabs"
    )
    chk.add_argument("--key", required=True, type=Path,
                     help="the service-account JSON key (keep it under dev/)")
    chk.add_argument("--spreadsheet-id", required=True,
                     help="from the sheet's URL, between /d/ and /edit")
    psh = sub.add_parser(
        "push", help="replace the data tabs' VALUES from the ledger CSVs"
    )
    psh.add_argument("--key", required=True, type=Path)
    psh.add_argument("--spreadsheet-id", required=True)
    psh.add_argument("--csv-dir", required=True, type=Path,
                     help="the directory `tools.ledger --out-dir` wrote")
    rpt = sub.add_parser(
        "report",
        help="(re)build the Dashboard/Weekly/Monthly tabs — by hand, never nightly",
    )
    rpt.add_argument("--key", required=True, type=Path)
    rpt.add_argument("--spreadsheet-id", required=True)
    args = parser.parse_args(argv)

    if args.command == "report":
        try:
            sheet, built = build_report(args.key, args.spreadsheet_id)
        except SheetsError as exc:
            print(f"sheets report FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"rebuilt {', '.join(built)} in {sheet.title!r}\n  {sheet.url}")
        return 0

    if args.command == "push":
        try:
            sheet, pushes = push(args.key, args.spreadsheet_id, args.csv_dir)
        except SheetsError as exc:
            print(f"sheets push FAILED: {exc}", file=sys.stderr)
            return 1
        for one in pushes:
            grew = " (grid grown)" if one.grew_grid else ""
            print(f"  {one.tab:12} {one.rows - 1:>6} row(s), "
                  f"{one.columns} column(s){grew}")
        print(f"pushed to {sheet.title!r}\n  {sheet.url}")
        return 0

    try:
        account, sheet = check(args.key, args.spreadsheet_id)
    except SheetsError as exc:
        print(f"sheets check FAILED: {exc}", file=sys.stderr)
        print(
            "\nIf Google says the caller has no permission: open the spreadsheet, "
            "Share, and give Editor to the key's client_email.",
            file=sys.stderr,
        )
        return 1
    print(f"signed in as {account.client_email}")
    print(f"spreadsheet: {sheet.title!r}\n  {sheet.url}")
    print(f"data tabs:   {', '.join(sheet.tabs)}")
    print("created:     " + (", ".join(sheet.created_tabs) or "nothing (all present)"))
    print("write access confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
