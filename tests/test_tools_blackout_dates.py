"""Discretionary-date merge behaviour in `argus.tools.blackout_dates`.

Scoped to the pure functions (`render`, `_summarize`,
`_refuse_if_any_event_is_empty`, `load_discretionary_dates`) — `main()` always
hits FRED and isn't exercised here. The property that matters: a regeneration
must be a no-op when no discretionary dates are queued (so today's committed
`blackout_dates.txt` renders identically), and it must never refuse to write
just because the discretionary file is empty or absent.
"""

from __future__ import annotations

from datetime import date

from argus.tools.blackout_dates import (
    _refuse_if_any_event_is_empty,
    _summarize,
    load_discretionary_dates,
    render,
)

_REQUIRED = {
    "FOMC": [date(2026, 9, 16)],
    "CPI": [date(2026, 9, 11)],
    "NFP": [date(2026, 9, 4)],
    "PCE": [date(2026, 9, 26)],
}


def test_load_discretionary_dates_missing_file_is_empty_not_an_error(tmp_path):
    assert load_discretionary_dates(tmp_path / "nope.txt") == []


def test_load_discretionary_dates_parses_same_format_as_fomc(tmp_path):
    p = tmp_path / "discretionary_blackout_dates.txt"
    p.write_text("# reason: scheduled outage\n2026-09-10\n\n2026-10-01\n")
    assert load_discretionary_dates(p) == [date(2026, 9, 10), date(2026, 10, 1)]


def test_refuse_does_not_fire_on_empty_discretionary():
    events = {**_REQUIRED, "DISCRETIONARY": []}
    _refuse_if_any_event_is_empty(events)  # must not raise


def test_refuse_still_fires_on_empty_required_event():
    events = {**_REQUIRED, "CPI": [], "DISCRETIONARY": []}
    try:
        _refuse_if_any_event_is_empty(events)
    except Exception as exc:  # BlackoutError
        assert "CPI" in str(exc)
        assert "DISCRETIONARY" not in str(exc)
    else:
        raise AssertionError("expected a refusal for an empty required event")


def test_render_omits_discretionary_section_when_empty():
    """A deploy with no discretionary dates queued renders byte-identical to
    the pre-discretionary output — the no-op property the merge depends on."""
    events = {**_REQUIRED, "DISCRETIONARY": []}
    with_empty = render(events, generated_on=date(2026, 9, 4), months=6)
    without_key = render(
        {k: v for k, v in events.items() if k != "DISCRETIONARY"},
        generated_on=date(2026, 9, 4),
        months=6,
    )
    assert with_empty == without_key
    assert "DISCRETIONARY" not in with_empty


def test_render_includes_discretionary_section_last_and_deduped():
    events = {
        **_REQUIRED,
        "DISCRETIONARY": [date(2026, 9, 10), date(2026, 9, 16)],  # 09-16 == FOMC date
    }
    out = render(events, generated_on=date(2026, 9, 4), months=6)
    fomc_idx = out.index("# --- FOMC ---")
    disc_idx = out.index("# --- DISCRETIONARY ---")
    assert fomc_idx < disc_idx
    # the date shared with FOMC is written once, under FOMC, not duplicated here
    disc_section = out[disc_idx:]
    assert disc_section.count("2026-09-16") == 0
    assert "2026-09-10" in disc_section


def test_summarize_discretionary_empty_is_neutral_not_alarmed():
    events = {**_REQUIRED, "DISCRETIONARY": []}
    notes = _summarize(events, date(2026, 12, 4))
    disc_note = next(n for n in notes if "DISCRETIONARY" in n)
    assert "none queued" in disc_note
    assert "NONE —" not in disc_note


def test_summarize_discretionary_populated():
    events = {**_REQUIRED, "DISCRETIONARY": [date(2026, 9, 10)]}
    notes = _summarize(events, date(2026, 12, 4))
    disc_note = next(n for n in notes if "DISCRETIONARY" in n)
    assert "1 dates" in disc_note
    assert "2026-09-10" in disc_note


# -- omitting a release family by policy (2026-09-28) ------------------------------
#
# docs/decisions/2026-09-28-trading-the-8-30-release-days.md. The properties: an
# omitted family never loads (entries allowed), never shadows another family's date,
# is still held to the emptiness guard, FOMC can never be omitted, and with nothing
# omitted the output is byte-identical to before.

import pytest  # noqa: E402

from argus.config import load_blackout_dates  # noqa: E402
from argus.tools import blackout_dates as bd  # noqa: E402

_OMIT_830 = frozenset({"CPI", "NFP", "PCE"})


def test_nothing_omitted_renders_byte_identical_to_before():
    events = {**_REQUIRED, "DISCRETIONARY": [date(2026, 9, 10)]}
    assert render(events, generated_on=date(2026, 9, 4), months=6) == render(
        events, generated_on=date(2026, 9, 4), months=6, omitted=frozenset()
    )


def test_an_omitted_family_never_loads_and_fomc_still_does(tmp_path):
    out = render(_REQUIRED, generated_on=date(2026, 9, 4), months=6, omitted=_OMIT_830)
    path = tmp_path / "blackout_dates.txt"
    path.write_text(out)
    assert load_blackout_dates(path) == {date(2026, 9, 16)}, "only FOMC is a blackout"
    # ...and the omitted dates are still in the file, as a record
    for name, (d,) in (("CPI", _REQUIRED["CPI"]), ("NFP", _REQUIRED["NFP"]), ("PCE", _REQUIRED["PCE"])):
        assert f"# --- {name}: OMITTED BY POLICY" in out
        assert f"# {d.isoformat()}" in out
    assert "--omit CPI --omit NFP --omit PCE" in out, "the file names the flags that reproduce it"


def test_an_omitted_family_never_shadows_a_discretionary_date(tmp_path):
    """A date shared by an omitted CPI and a discretionary avoidance stays a blackout."""
    shared = _REQUIRED["CPI"][0]
    events = {**_REQUIRED, "DISCRETIONARY": [shared]}
    path = tmp_path / "blackout_dates.txt"
    path.write_text(render(events, generated_on=date(2026, 9, 4), months=6, omitted=_OMIT_830))
    assert shared in load_blackout_dates(path)


def test_fomc_can_never_be_omitted():
    with pytest.raises(bd.BlackoutError):
        render(_REQUIRED, generated_on=date(2026, 9, 4), months=6, omitted=frozenset({"FOMC"}))
    with pytest.raises(SystemExit):
        bd.main(["--omit", "FOMC", "--check"])


def test_an_omitted_family_is_still_held_to_the_emptiness_guard():
    """Omission is policy, never a fix for a broken query: an empty CPI still refuses."""
    with pytest.raises(bd.BlackoutError, match="CPI"):
        _refuse_if_any_event_is_empty({**_REQUIRED, "CPI": []})


def test_main_writes_the_omission_and_summarizes_it(tmp_path, monkeypatch, capsys):
    (tmp_path / "fomc_dates.txt").write_text("2026-10-28\n")
    fred = {10: [date(2026, 10, 14)], 50: [date(2026, 10, 2)], 54: [date(2026, 9, 30)]}
    monkeypatch.setattr(bd, "_api_key", lambda: "k")
    monkeypatch.setattr(bd, "et_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr(bd, "fetch_release_dates",
                        lambda release_id, **_: fred[release_id])
    out = tmp_path / "blackout_dates.txt"
    assert bd.main(["--out", str(out), "--omit", "CPI", "--omit", "NFP", "--omit", "PCE"]) == 0
    assert load_blackout_dates(out) == {date(2026, 10, 28)}
    printed = capsys.readouterr().out
    assert "OMITTED by policy" in printed and "TOTAL 1 distinct" in printed
    # --check with the same flags agrees; without them it differs (the safe direction)
    assert bd.main(["--out", str(out), "--omit", "CPI", "--omit", "NFP", "--omit", "PCE", "--check"]) == 0
    assert bd.main(["--out", str(out), "--check"]) == 1
