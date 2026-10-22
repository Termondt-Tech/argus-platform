"""Generate the macro-event blackout date file `[events]` points at.

Fetches CPI / NFP / PCE release dates from FRED and merges them with two
hand-maintained lists — FOMC decision dates and discretionary avoidance dates —
writing the one-ISO-date-per-line format `config.load_blackout_dates` reads.

    ./.venv/bin/python -m argus.tools.blackout_dates --out blackout_dates.txt
    ./.venv/bin/python -m argus.tools.blackout_dates --out blackout_dates.txt --months 12
    ./.venv/bin/python -m argus.tools.blackout_dates --out blackout_dates.txt --check
    ./.venv/bin/python -m argus.tools.blackout_dates --out blackout_dates.txt --add-fomc 2027-01-27
    ./.venv/bin/python -m argus.tools.blackout_dates --out blackout_dates.txt --add-discretionary 2026-09-10
    ./.venv/bin/python -m argus.tools.blackout_dates --out blackout_dates.txt --omit CPI --omit NFP --omit PCE

RELATIONSHIP TO TOMIC. This reuses the PATTERN only — FRED's `release/dates`
endpoint and the release ids (CPI=10, NFP=50, PCE=54), plus the finding that FOMC
decisions have no FRED release id and must stay manual. Nothing is imported from
TOMIC, nothing is written to it, and the credential is Argus's OWN
`ARGUS_FRED_API_KEY`, never TOMIC's `FRED_API_KEY`. Per CLAUDE.md's "Relationship
to TOMIC", the pattern is shared and the code and credentials are not.

WHY FOMC IS MANUAL. FRED publishes DATA RELEASES; an FOMC rate decision is not
one, so there is no release id to query and no authoritative API to hit. Scraping
federalreserve.gov was considered and rejected for the same reason TOMIC rejected
it: a silent parsing break looks exactly like "no upcoming meetings", which is the
failure that would quietly un-blackout the single most important day of the
quarter. The Fed publishes the calendar ~a year ahead, so hand-maintaining ~8
dates a year is cheap and verifiable. They live in `--fomc-file` (default
`fomc_dates.txt` next to the output) and are merged in on every run.

DISCRETIONARY DATES ARE OPTIONAL WHERE FOMC IS NOT. They live in
`--discretionary-file` (default `discretionary_blackout_dates.txt` next to the
output) — same format, merged in the same way — but a missing or empty file is
the ORDINARY state, never an error: most days nobody has declared a one-off
avoidance (a geopolitical event, an operator's own day off), where FOMC/CPI/NFP/
PCE are scheduled well ahead and emptiness there always means the query broke.
So this list is deliberately EXCLUDED from `_refuse_if_any_event_is_empty` — it
is summarized, never enforced. This exists so a regeneration can never silently
drop a hand-added avoidance date the way editing the generated output directly
would (the header below says "do not hand-edit" for a reason: `render()`
rewrites the whole file from these inputs every run).

Never repurpose `[calendar] holidays` for this — that list means "the exchange
is physically closed" and drives session scheduling and 0DTE expiry resolution.
A discretionary avoidance day is "market's open, we choose not to enter" and
belongs here instead, where it suppresses NEW entries only; anything already
open still gets its watchdog, virtual stops and flatten.

WHY STDLIB `urllib` AND NOT `requests`. Argus's runtime is stdlib-only apart from
the tastytrade SDK, and the image is built `--require-hashes` for a minimal
auditable supply chain (CLAUDE.md, Build/test). This is one GET against one fixed
host; it does not justify a dependency. TOMIC's script uses `requests` because
TOMIC already depends on it.

OMITTING A RELEASE FAMILY (2026-09-28). `--omit CPI|NFP|PCE` takes a family OUT of
the blackout, so entries ARE taken on its release days, and is a POLICY decision,
never a fix for a broken query. FOMC can never be omitted: it is an intraday 14:00
event, the one the blackout's evidence is about, and CPI/NFP/PCE all print at 08:30,
before the session opens. An omitted family is still FETCHED and still held to
`_refuse_if_any_event_is_empty`, so a broken FRED query stays loud whatever the
policy is. It is written into the file as a COMMENTED section (its dates prefixed
`#`, so `load_blackout_dates` never reads them and they never shadow another
family's date), with a header naming the flags that reproduce it: the file records
the policy that made it, and a regeneration without the flags puts the family back,
the safe direction.
-> docs/decisions/2026-09-28-trading-the-8-30-release-days.md

THIS TOOL IS OPERATIONAL, NOT RUNTIME. Nothing in the running process calls it —
`python -m argus` only ever READS the file it produces. Run it when the calendar
is extended (a few times a year), commit the result, and deploy it alongside
the config; the file is bind-mounted next to it, so it is versioned by
git on the droplet rather than by the image tag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

from argus.bootstrap import load_env
from argus.clock import et_today
from argus.config import ConfigError, load_blackout_dates

FRED_URL = "https://api.stlouisfed.org/fred/release/dates"

# FRED release ids. Same three TOMIC cross-checks against; there is deliberately
# no FOMC entry here (see the module docstring).
FRED_RELEASE_IDS = {"CPI": 10, "NFP": 50, "PCE": 54}

#: The families `--omit` accepts: the 08:30 ET releases. FOMC is deliberately not one
#: (see the module docstring), and nothing else is a family.
OMITTABLE = tuple(FRED_RELEASE_IDS)

FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

DEFAULT_MONTHS = 12
HTTP_TIMEOUT_S = 30.0


class BlackoutError(RuntimeError):
    """Generation failed. Never write a partial file on this path."""


def _api_key() -> str:
    key = os.environ.get("ARGUS_FRED_API_KEY", "").strip()
    if not key:
        raise BlackoutError(
            "ARGUS_FRED_API_KEY is not set. This is Argus's OWN FRED key — do not "
            "reuse TOMIC's FRED_API_KEY. Get one free at "
            "https://fredaccount.stlouisfed.org/apikeys and put it in .env"
        )
    return key


def fetch_release_dates(
    release_id: int, *, api_key: str, start: date, end: date
) -> list[date]:
    """CPI/NFP/PCE release dates in [start, end] for one FRED release id.

    `include_release_dates_with_no_data=true` IS REQUIRED AND IS COUNTERINTUITIVE.
    "No data" does not mean "no such release" — it means the release date is
    SCHEDULED BUT NOT YET PUBLISHED, which is precisely and exclusively what a
    forward-looking blackout list is made of. With it `false` this endpoint
    returns `count: 0` for any future window (measured against the live API,
    2026-08-04), so every event silently reads as "no dates" and the blackout
    covers nothing. The failure is silent by construction — an empty list is
    indistinguishable from a quiet calendar — so the guard is `_summarize`'s
    per-event NONE report plus the loud refusal below.

    `realtime_start`/`realtime_end` bound the window server-side, so the response
    holds only the dates asked for rather than the release's entire history (CPI
    alone has ~950 dates back to 1949, which a client-side filter against a
    default row limit would eventually truncate from the FUTURE end).

    The API key is passed as a query parameter because FRED offers no header
    auth — hence `_redact` on every error path, so a failing request cannot put
    the key in a traceback, a log line, or a CI transcript.
    """
    params = {
        "release_id": str(release_id),
        "api_key": api_key,
        "file_type": "json",
        "realtime_start": start.isoformat(),
        "realtime_end": end.isoformat(),
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
        "limit": "1000",
    }
    url = f"{FRED_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = " (check ARGUS_FRED_API_KEY)" if exc.code in (400, 401, 403) else ""
        raise BlackoutError(
            f"FRED returned HTTP {exc.code} for release {release_id}{hint}"
        ) from None
    except urllib.error.URLError as exc:
        raise BlackoutError(
            f"could not reach FRED for release {release_id}: {_redact(str(exc.reason), api_key)}"
        ) from None
    except json.JSONDecodeError:
        raise BlackoutError(
            f"FRED returned a non-JSON body for release {release_id}"
        ) from None

    rows = payload.get("release_dates")
    if not isinstance(rows, list):
        raise BlackoutError(
            f"FRED response for release {release_id} has no release_dates array"
        )
    out: list[date] = []
    for row in rows:
        raw = row.get("date") if isinstance(row, dict) else None
        if not raw:
            continue
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            # Fail closed on a shape change rather than silently dropping a date:
            # a missing CPI print is exactly the day this file exists to suppress.
            raise BlackoutError(
                f"FRED release {release_id} returned an unparseable date {raw!r}"
            ) from None
        if start <= parsed <= end:
            out.append(parsed)
    return sorted(set(out))


def _redact(text: str, api_key: str) -> str:
    return text.replace(api_key, "<ARGUS_FRED_API_KEY>") if api_key else text


def load_fomc_dates(path: Path) -> list[date]:
    """Hand-maintained FOMC decision dates, same file format as the output.

    Use the SECOND (decision) day of each two-day meeting — the day the statement
    lands at 14:00 ET and realized vol actually resolves. Missing file is an
    error, not an empty list: silently generating a blackout file with no FOMC
    dates is the single worst outcome this tool has.
    """
    if not path.exists():
        raise BlackoutError(
            f"FOMC date file not found: {path}\n"
            f"FOMC decisions have no FRED release id and must be maintained by hand.\n"
            f"Create it with one ISO date per line (the DECISION day of each meeting), "
            f"from {FOMC_CALENDAR_URL}"
        )
    try:
        return sorted(load_blackout_dates(path))
    except ConfigError as exc:
        raise BlackoutError(str(exc)) from None


def load_discretionary_dates(path: Path) -> list[date]:
    """Hand-maintained one-off avoidance dates, same file format as the output.

    Unlike `load_fomc_dates`, a missing file returns EMPTY rather than raising —
    most days nobody has declared a discretionary avoidance, and that is the
    ordinary state, not a bug. A malformed line in a file that DOES exist still
    raises, same as everywhere else: a typo'd date silently failing to load is
    the one failure mode this file exists to prevent.
    """
    if not path.exists():
        return []
    try:
        return sorted(load_blackout_dates(path))
    except ConfigError as exc:
        raise BlackoutError(str(exc)) from None


def render(
    events: dict[str, list[date]],
    *,
    generated_on: date,
    months: int,
    omitted: frozenset[str] = frozenset(),
) -> str:
    """The output file: a commented header, then one ISO date per line.

    Dates are grouped under a comment naming the event that produced them, which
    `load_blackout_dates` ignores and a human reading the file very much does not.
    Sorted and deduped — a date carrying two events (a CPI print on an FOMC day,
    or a discretionary date that lands on one) is written once, under the first
    event that claimed it. DISCRETIONARY is deliberately LAST: it is the
    operator's own layer on top of the automated macro calendar, and an empty
    section is skipped entirely — so a deploy with no discretionary dates queued
    renders byte-identical to before this category existed.

    An `omitted` family (2026-09-28) is rendered as a COMMENTED section, after a
    header line naming the flags that reproduce the file, and never claims a date:
    its dates are a record, and one it shares with another family stays active under
    that family. With nothing omitted the output is byte-identical to before.
    """
    unknown = sorted(set(omitted) - set(OMITTABLE))
    if unknown:
        raise BlackoutError(f"cannot omit {', '.join(unknown)}: only {', '.join(OMITTABLE)}")
    lines = [
        "# Argus macro-event + discretionary entry blackout dates.",
        "# GENERATED by `python -m argus.tools.blackout_dates` — do not hand-edit;",
        "# add FOMC dates via --fomc-file, one-off avoidance dates via",
        "# --discretionary-file, and regenerate.",
        f"# generated {generated_on.isoformat()}, horizon {months} months",
        "#",
        "# One ISO date per line. Blank lines and # comments are ignored.",
        "# Argus refuses ALL new entries on these dates (whole-day blackout).",
    ]
    if omitted:
        names = [n for n in OMITTABLE if n in omitted]
        lines += [
            "#",
            f"# OMITTED BY POLICY: {', '.join(names)}. Entries ARE taken on these release",
            "# days; their dates are listed below as comments, a record that never loads.",
            "# Regenerate with " + " ".join(f"--omit {n}" for n in names) + " to keep the policy.",
        ]
    lines.append("")
    seen: set[date] = set()
    for name in ("FOMC", "CPI", "NFP", "PCE", "DISCRETIONARY"):
        if name in omitted:
            record = sorted(events.get(name, []))
            if record:
                lines.append(f"# --- {name}: OMITTED BY POLICY, entries allowed ---")
                lines.extend(f"# {d.isoformat()}" for d in record)
                lines.append("")
            continue
        fresh = [d for d in sorted(events.get(name, [])) if d not in seen]
        if not fresh:
            continue
        seen.update(fresh)
        lines.append(f"# --- {name} ---")
        lines.extend(d.isoformat() for d in fresh)
        lines.append("")
    return "\n".join(lines) + "\n"


def _add_months(d: date, months: int) -> date:
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    first_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return d.replace(year=year, month=month, day=min(d.day, (first_next - timedelta(days=1)).day))


def _refuse_if_any_event_is_empty(events: dict[str, list[date]]) -> None:
    """Refuse to build a blackout file that silently covers nothing.

    THIS GUARD EXISTS BECAUSE THE FAILURE ALREADY HAPPENED. The first live run
    returned zero CPI/NFP/PCE dates — `include_release_dates_with_no_data` was
    `false`, which excludes exactly the not-yet-published releases a forward
    blackout is made of. The tool reported "NONE" for three of four events and
    would still have written the file, leaving `[events] enabled = true` pointing
    at a list that suppressed nothing on CPI, NFP and PCE days.

    That is the whole failure mode this feature exists to prevent, one level up: a
    blackout that covers nothing is indistinguishable, from inside the running
    process, from a calendar with no scheduled events. Every one of the four
    sources publishes on a monthly-or-tighter cadence, so an empty list over any
    real horizon is a broken query, never a quiet calendar.

    DISCRETIONARY is deliberately EXCLUDED from this tuple. Its emptiness is the
    ordinary state — most days nobody has declared a one-off avoidance — so it
    must never make an otherwise-healthy regeneration refuse to write.
    """
    empty = sorted(name for name in ("FOMC", "CPI", "NFP", "PCE") if not events.get(name))
    if not empty:
        return
    raise BlackoutError(
        f"no dates found for {', '.join(empty)} — refusing to write a blackout file "
        f"that would not suppress entries on those days.\n"
        f"All four events publish at least monthly, so an empty list over this "
        f"horizon means the query is wrong or the source changed, not that the "
        f"calendar is quiet.\n"
        f"FOMC comes from the --fomc-file; CPI/NFP/PCE come from FRED."
    )


def _summarize(
    events: dict[str, list[date]], horizon_end: date, omitted: frozenset[str] = frozenset()
) -> list[str]:
    """Per-event coverage. A source whose last date falls well short of the
    horizon is the failure mode that matters: the file looks populated, and the
    blackout quietly stops covering that event partway through.

    DISCRETIONARY prints with neutral wording when empty ("none queued") rather
    than the required events' alarmed "NONE — will not cover" — an empty
    discretionary list is the ordinary state, not a broken query, and must not
    read as one.
    """
    notes = []
    for name in ("FOMC", "CPI", "NFP", "PCE"):
        dates = sorted(events.get(name, []))
        if not dates:
            notes.append(f"  {name:<14} NONE — blackout will not cover this event")
            continue
        gap = (horizon_end - dates[-1]).days
        flag = "  <-- runs out early" if gap > 45 else ""
        policy = "  OMITTED by policy: entries allowed" if name in omitted else ""
        notes.append(
            f"  {name:<14} {len(dates):>3} dates, {dates[0]} .. {dates[-1]}{flag}{policy}"
        )
    dates = sorted(events.get("DISCRETIONARY", []))
    if not dates:
        notes.append(f"  {'DISCRETIONARY':<14} none queued (this is normal)")
    else:
        notes.append(
            f"  {'DISCRETIONARY':<14} {len(dates):>3} dates, {dates[0]} .. {dates[-1]}"
        )
    return notes


def main(argv: Iterable[str] | None = None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description=(
            "Generate Argus's macro-event blackout date file "
            "(FRED + manual FOMC + manual discretionary)"
        )
    )
    parser.add_argument("--out", default="blackout_dates.txt", help="output file path")
    parser.add_argument(
        "--fomc-file",
        default=None,
        help="hand-maintained FOMC decision dates (default: fomc_dates.txt beside --out)",
    )
    parser.add_argument(
        "--discretionary-file",
        default=None,
        help=(
            "hand-maintained one-off avoidance dates (default: "
            "discretionary_blackout_dates.txt beside --out). Optional — a "
            "missing file means no discretionary dates, not an error."
        ),
    )
    parser.add_argument(
        "--months", type=int, default=DEFAULT_MONTHS, help="forward horizon in months"
    )
    parser.add_argument(
        "--add-fomc",
        action="append",
        default=[],
        metavar="YYYY-MM-DD",
        help="append a hand-verified FOMC date to --fomc-file before generating",
    )
    parser.add_argument(
        "--add-discretionary",
        action="append",
        default=[],
        metavar="YYYY-MM-DD",
        help="append a one-off avoidance date to --discretionary-file before generating",
    )
    parser.add_argument(
        "--omit",
        action="append",
        default=[],
        choices=OMITTABLE,
        metavar="FAMILY",
        help=(
            "take a release family OUT of the blackout (entries allowed on its days): "
            f"one of {', '.join(OMITTABLE)}, repeatable. POLICY — FOMC can never be "
            "omitted. Still fetched and checked; written as a commented record"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what WOULD change and exit non-zero if it differs; writes nothing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    omitted = frozenset(args.omit)

    out_path = Path(args.out)
    fomc_path = (
        Path(args.fomc_file) if args.fomc_file else out_path.parent / "fomc_dates.txt"
    )
    discretionary_path = (
        Path(args.discretionary_file)
        if args.discretionary_file
        else out_path.parent / "discretionary_blackout_dates.txt"
    )

    try:
        for raw in args.add_fomc:
            try:
                parsed = date.fromisoformat(raw)
            except ValueError:
                raise BlackoutError(f"--add-fomc: not an ISO date: {raw!r}") from None
            existing = load_fomc_dates(fomc_path) if fomc_path.exists() else []
            if parsed in existing:
                print(f"FOMC {parsed} already present in {fomc_path}")
                continue
            with fomc_path.open("a") as fh:
                fh.write(f"{parsed.isoformat()}\n")
            print(f"added FOMC {parsed} to {fomc_path}")

        for raw in args.add_discretionary:
            try:
                parsed = date.fromisoformat(raw)
            except ValueError:
                raise BlackoutError(
                    f"--add-discretionary: not an ISO date: {raw!r}"
                ) from None
            existing = load_discretionary_dates(discretionary_path)
            if parsed in existing:
                print(f"discretionary date {parsed} already present in {discretionary_path}")
                continue
            with discretionary_path.open("a") as fh:
                fh.write(f"{parsed.isoformat()}\n")
            print(f"added discretionary date {parsed} to {discretionary_path}")

        today = et_today()
        horizon_end = _add_months(today, args.months)
        api_key = _api_key()

        events: dict[str, list[date]] = {
            # Past FOMC/discretionary dates are filtered out the same way FRED's
            # window filters the others, so the file stays forward-looking.
            "FOMC": [d for d in load_fomc_dates(fomc_path) if today <= d <= horizon_end],
            "DISCRETIONARY": [
                d
                for d in load_discretionary_dates(discretionary_path)
                if today <= d <= horizon_end
            ],
        }
        for name, release_id in FRED_RELEASE_IDS.items():
            events[name] = fetch_release_dates(
                release_id, api_key=api_key, start=today, end=horizon_end
            )

    except BlackoutError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # The per-event breakdown prints BEFORE the emptiness refusal, so a refusal
    # arrives with the evidence for it rather than as a bare error.
    print(f"horizon {today} .. {horizon_end} ({args.months} months)")
    for note in _summarize(events, horizon_end, omitted):
        print(note)
    total = len({d for name, dates in events.items() if name not in omitted for d in dates})
    print(f"  TOTAL {total} distinct blackout dates"
          + (f" ({', '.join(sorted(omitted))} omitted by policy)" if omitted else ""))

    try:
        _refuse_if_any_event_is_empty(events)
        rendered = render(events, generated_on=today, months=args.months, omitted=omitted)
    except BlackoutError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2

    current = out_path.read_text() if out_path.exists() else None
    if args.check:
        if current == rendered:
            print(f"\n{out_path} is up to date")
            return 0
        print(f"\n{out_path} DIFFERS from what would be generated (run without --check)")
        return 1

    out_path.write_text(rendered)
    print(f"\nwrote {out_path}" + ("" if current is None else " (updated)"))
    print("Commit this file and deploy it alongside the profile config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
