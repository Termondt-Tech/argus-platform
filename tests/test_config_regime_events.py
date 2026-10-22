"""[regime] / [events] parsing and load_blackout_dates.

Both sections are OPTIONAL, for different reasons, and the tests distinguish
them: `[regime]` absent is the SHIPPED state (thresholds uncalibrated), while
`[events]` absent is only the pre-upgrade state.
"""

from __future__ import annotations

from datetime import date

import pytest
from support import config_dict

from argus.config import Config, ConfigError, load_blackout_dates


def _regime_section(**over) -> dict:
    base = {
        "atm_iv_low_max": 0.15,
        "atm_iv_high_min": 0.30,
        "max_downside_skew_for_puts": 0.002,
        "max_upside_skew_for_calls": 0.002,
        "size_multiplier_low": 1.0,
        "size_multiplier_mid": 0.75,
        "size_multiplier_high": 0.5,
    }
    base.update(over)
    return base


def _load(**sections) -> Config:
    # `config_dict` merges dict patches into an existing section; [regime] and
    # [events] do not exist in the base fixture, so they land as new top-level keys.
    return Config.from_dict(config_dict(**sections))


# --------------------------------------------------------------------------
# Optionality
# --------------------------------------------------------------------------


def test_both_sections_absent_is_valid():
    cfg = _load()
    assert cfg.regime is None
    assert cfg.events is None


def test_regime_absent_is_the_shipped_state_of_the_real_config():
    """`argus.sandbox.toml` must NOT carry a [regime] section: adding one turns
    uncalibrated research thresholds into live sizing and side-gating."""
    assert Config.load("argus.sandbox.toml").regime is None
    assert Config.load("argus.dev.toml").regime is None


def test_events_present_and_enabled_in_the_real_configs():
    """The blackout ships ON in every profile — live included, which is the one
    that matters and the one no session has ever started."""
    for path in ("argus.sandbox.toml", "argus.dev.toml", "argus.live.toml"):
        events = Config.load(path).events
        assert events is not None and events.enabled, path


# --------------------------------------------------------------------------
# [regime] validation
# --------------------------------------------------------------------------


def test_regime_parses():
    cfg = _load(regime=_regime_section())
    assert cfg.regime is not None
    assert cfg.regime.atm_iv_low_max == 0.15
    assert cfg.regime.size_multiplier_high == 0.5


def test_regime_rejects_inverted_thresholds():
    with pytest.raises(ConfigError, match="atm_iv_low_max must be <"):
        _load(regime=_regime_section(atm_iv_low_max=0.4, atm_iv_high_min=0.2))


def test_regime_rejects_equal_thresholds():
    with pytest.raises(ConfigError, match="atm_iv_low_max must be <"):
        _load(regime=_regime_section(atm_iv_low_max=0.2, atm_iv_high_min=0.2))


@pytest.mark.parametrize(
    "key", ["size_multiplier_low", "size_multiplier_mid", "size_multiplier_high"]
)
def test_regime_refuses_a_multiplier_above_one(key):
    """This section scales risk DOWN, never up. A >1 multiplier would let an
    uncalibrated research number inflate size past the operator's decided rail."""
    with pytest.raises(ConfigError, match="scales risk down, never up"):
        _load(regime=_regime_section(**{key: 1.5}))


def test_regime_allows_exactly_one():
    assert _load(regime=_regime_section(size_multiplier_low=1.0)).regime is not None


@pytest.mark.parametrize(
    "key",
    [
        "atm_iv_low_max",
        "atm_iv_high_min",
        "max_downside_skew_for_puts",
        "max_upside_skew_for_calls",
        "size_multiplier_low",
        "size_multiplier_mid",
        "size_multiplier_high",
    ],
)
def test_regime_is_all_or_nothing(key):
    """A half-specified regime gate is worse than no gate: no per-key defaults."""
    section = _regime_section()
    del section[key]
    with pytest.raises(ConfigError, match=f"regime.{key}"):
        _load(regime=section)


@pytest.mark.parametrize("key", ["atm_iv_low_max", "size_multiplier_mid"])
def test_regime_rejects_non_positive(key):
    with pytest.raises(ConfigError, match="must be > 0"):
        _load(regime=_regime_section(**{key: 0.0}))


# --------------------------------------------------------------------------
# [events] validation
# --------------------------------------------------------------------------


def test_events_parses():
    cfg = _load(events={"enabled": True, "blackout_dates_path": "b.txt"})
    assert cfg.events is not None
    assert cfg.events.enabled is True
    assert cfg.events.blackout_dates_path == "b.txt"


def test_events_can_be_disabled_while_present():
    cfg = _load(events={"enabled": False, "blackout_dates_path": "b.txt"})
    assert cfg.events is not None and cfg.events.enabled is False


def test_events_requires_both_keys():
    with pytest.raises(ConfigError, match="events.blackout_dates_path"):
        _load(events={"enabled": True})
    with pytest.raises(ConfigError, match="events.enabled"):
        _load(events={"blackout_dates_path": "b.txt"})


def test_events_enabled_must_be_a_real_bool():
    with pytest.raises(ConfigError, match="events.enabled"):
        _load(events={"enabled": "true", "blackout_dates_path": "b.txt"})


# --------------------------------------------------------------------------
# load_blackout_dates
# --------------------------------------------------------------------------


def test_load_blackout_dates_parses_comments_and_blanks(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text(
        "# a comment\n"
        "\n"
        "2026-09-16\n"
        "   \n"
        "  2026-10-28  \n"
        "# --- CPI ---\n"
        "2026-12-10\n"
    )
    assert load_blackout_dates(p) == frozenset(
        {date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 10)}
    )


def test_load_blackout_dates_missing_file_raises(tmp_path):
    """Fail closed. `enabled = true` against no file means the operator believes
    entries are suppressed while nothing is."""
    with pytest.raises(ConfigError, match="blackout dates file not found"):
        load_blackout_dates(tmp_path / "nope.txt")


def test_load_blackout_dates_malformed_line_raises(tmp_path):
    """A silently-dropped FOMC date defeats the entire point."""
    p = tmp_path / "b.txt"
    p.write_text("2026-09-16\nnot-a-date\n")
    with pytest.raises(ConfigError, match="not a valid ISO date"):
        load_blackout_dates(p)


def test_load_blackout_dates_reports_the_line_number(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("# header\n\n2026-09-16\n2026-13-99\n")
    with pytest.raises(ConfigError, match=r":4:"):
        load_blackout_dates(p)


def test_load_blackout_dates_empty_file_is_an_empty_set(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("# nothing scheduled\n")
    assert load_blackout_dates(p) == frozenset()


def test_the_shipped_blackout_file_loads():
    """The committed file must actually parse — it is loaded at session start, so
    a typo in it is a refused session, not a warning.

    Since 2026-09-28 the 08:30 releases (CPI, NFP, PCE) are OMITTED BY POLICY —
    docs/decisions/2026-09-28-trading-the-8-30-release-days.md — so a CPI day is a
    commented record, not a blackout, and FOMC stays. Pinned both ways, so a
    regeneration that forgets `--omit` fails here rather than silently re-blacking
    the release days (and one that omits FOMC can't happen: the tool refuses it)."""
    dates = load_blackout_dates("blackout_dates.txt")
    assert date(2026, 12, 9) in dates          # FOMC
    assert date(2026, 12, 10) not in dates     # CPI: omitted by policy, entries allowed
    text = open("blackout_dates.txt").read()
    assert "# 2026-12-10" in text, "an omitted family is still listed, as a record"
    assert "--omit CPI --omit NFP --omit PCE" in text
    future_fomc = {d for d in load_blackout_dates("fomc_dates.txt") if d >= date(2026, 9, 28)}
    assert future_fomc and future_fomc <= dates, "every upcoming FOMC decision is blacked out"


def test_the_shipped_fomc_file_loads():
    dates = load_blackout_dates("fomc_dates.txt")
    assert date(2026, 7, 29) in dates      # the measured worst shadow day
    assert len(dates) == 8                 # 8 FOMC meetings/year


# --------------------------------------------------------------------------
# The generator's silent-failure guard
# --------------------------------------------------------------------------


def test_generator_refuses_a_blackout_file_covering_nothing():
    """REGRESSION. The first live run returned zero CPI/NFP/PCE dates because
    `include_release_dates_with_no_data` was false — which excludes exactly the
    not-yet-published releases a forward blackout is made of. It reported "NONE"
    and would still have written the file, leaving `enabled = true` pointing at a
    list that suppressed nothing on those days."""
    from argus.tools.blackout_dates import BlackoutError, _refuse_if_any_event_is_empty

    with pytest.raises(BlackoutError, match="CPI, NFP, PCE"):
        _refuse_if_any_event_is_empty(
            {"FOMC": [date(2026, 9, 16)], "CPI": [], "NFP": [], "PCE": []}
        )


def test_generator_refuses_when_only_fomc_is_missing():
    from argus.tools.blackout_dates import BlackoutError, _refuse_if_any_event_is_empty

    with pytest.raises(BlackoutError, match="FOMC"):
        _refuse_if_any_event_is_empty(
            {"FOMC": [], "CPI": [date(2026, 8, 12)], "NFP": [date(2026, 8, 7)],
             "PCE": [date(2026, 8, 26)]}
        )


def test_generator_accepts_a_fully_populated_calendar():
    from argus.tools.blackout_dates import _refuse_if_any_event_is_empty

    _refuse_if_any_event_is_empty(
        {name: [date(2026, 9, 16)] for name in ("FOMC", "CPI", "NFP", "PCE")}
    )


def test_fred_query_requests_unpublished_release_dates():
    """The one parameter the whole generator hinges on, pinned by name. A future
    scheduled release has NO DATA yet, so `false` here returns count=0 for any
    forward window and the blackout silently covers nothing."""
    import inspect

    from argus.tools import blackout_dates

    src = inspect.getsource(blackout_dates.fetch_release_dates)
    assert '"include_release_dates_with_no_data": "true"' in src
