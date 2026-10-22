"""Config loading is fail-closed: bad values raise rather than defaulting."""

from __future__ import annotations

import pytest

import copy

from argus.config import Config, ConfigError, GcConfig
from support import CONFIG as BASE
from support import CONFIG



def _cfg(**overrides):
    import copy

    raw = copy.deepcopy(BASE)
    for section, patch in overrides.items():
        if isinstance(patch, dict):
            raw[section].update(patch)
        else:
            raw[section] = patch
    return raw


def test_loads_valid_config():
    cfg = Config.from_dict(_cfg())
    assert cfg.environment == "shadow"
    assert cfg.coa.response_window_ms == 100
    assert cfg.risk.position_size_pct == 0.02
    assert cfg.staleness.market_data_max_age_s == 2.0
    assert cfg.audit.tier2_queue_maxsize == 100
    assert cfg.heartbeat.interval_s == 0.1
    assert cfg.heartbeat.trip_on_sustained_stall is False


def test_profile_must_agree_with_the_environment_key():
    """The filename names the profile; the key remains the authority.

    This REPLACES `ARGUS_ENV` overriding `environment`. That override let an env
    var point the SANDBOX config at the live API, and it was fenced only by
    accident — `account_stream_max_age_s = 120 > 30` tripped the live ceiling, and
    that 120 is a cert artifact that could legitimately drop at any time.
    """
    cfg = Config.from_dict(_cfg(environment="sandbox"), profile="sandbox")
    assert cfg.environment == "sandbox"
    with pytest.raises(ConfigError, match="names profile"):
        Config.from_dict(_cfg(environment="sandbox"), profile="live")


def test_an_unprofiled_path_asserts_nothing():
    """`--config /tmp/scratch.toml` and the pre-rename `argus.toml` stay legal."""
    cfg = Config.from_dict(_cfg(environment="sandbox"), profile=None)
    assert cfg.environment == "sandbox"


def test_a_config_that_is_a_directory_refuses_like_a_missing_one(tmp_path):
    """Reachable through the DEPLOY, not through a typo: a bind mount whose host
    path does not exist creates a DIRECTORY at the container path, so `ARGUS_ENV`
    naming a profile with no `argus.<profile>.toml` beside the compose file lands
    here rather than on `FileNotFoundError`. Same fail-closed sentence — the
    loader's refusals are the process's, and a bare `IsADirectoryError` traceback
    reads as a crash rather than as a misconfiguration."""
    (tmp_path / "argus.live.toml").mkdir()
    with pytest.raises(ConfigError, match="not readable"):
        Config.load(tmp_path / "argus.live.toml")


def test_dev_may_never_be_live():
    """`dev` is not a trading environment, so it takes no equality check — but it
    is the file most likely to be run by hand from a laptop."""
    with pytest.raises(ConfigError, match="never declare"):
        Config.from_dict(
            _cfg(environment="live", audit={"synchronous": "FULL"},
                 staleness={"account_stream_max_age_s": 30.0},
                 broker={"account_number": "5WX00000"}),
            profile="dev",
        )
    assert Config.from_dict(_cfg(environment="shadow"), profile="dev").environment == "shadow"


def test_marketdata_source_is_independent_of_environment():
    cfg = Config.from_dict(_cfg(environment="shadow", marketdata={"source": "live"}))
    assert cfg.environment == "shadow"          # orders stay in cert
    assert cfg.marketdata.source == "live"      # data comes from production


def test_rejects_unknown_marketdata_source():
    with pytest.raises(ConfigError, match="marketdata.source"):
        Config.from_dict(_cfg(marketdata={"source": "production"}))


def test_rejects_unknown_environment():
    with pytest.raises(ConfigError):
        Config.from_dict(_cfg(environment="production"))


def test_rejects_missing_coa_source():
    raw = _cfg()
    del raw["coa"]["source"]
    with pytest.raises(ConfigError, match="coa.source"):
        Config.from_dict(raw)


def test_rejects_nonpositive_daily_max_loss():
    with pytest.raises(ConfigError, match="daily_max_loss"):
        Config.from_dict(_cfg(risk={"daily_max_loss": 0}))


@pytest.mark.parametrize("bad", [0, -0.1, 1.5])
def test_rejects_out_of_range_position_size(bad):
    with pytest.raises(ConfigError, match="position_size_pct"):
        Config.from_dict(_cfg(risk={"position_size_pct": bad}))


def test_rejects_bool_for_numeric():
    # bool is an int subclass; config must not accept True where a number is meant.
    with pytest.raises(ConfigError):
        Config.from_dict(_cfg(coa={"response_window_ms": True}))


def test_rejects_alert_depth_above_maxsize():
    with pytest.raises(ConfigError, match="alert_depth"):
        Config.from_dict(_cfg(audit={"tier2_queue_maxsize": 100, "tier2_queue_alert_depth": 200}))


def test_missing_section_raises():
    raw = _cfg()
    del raw["risk"]
    with pytest.raises(ConfigError, match="risk"):
        Config.from_dict(raw)


def test_rejects_refresh_interval_at_or_above_max_age():
    # The liveness floor must be tighter than the tolerance, or the cache goes
    # stale between refreshes and the gate blocks every entry.
    with pytest.raises(ConfigError, match="account_refresh_s"):
        Config.from_dict(
            _cfg(staleness={"account_stream_max_age_s": 5.0, "account_refresh_s": 5.0})
        )


def test_rejects_refresh_timeout_at_or_above_the_refresh_interval():
    # A hung request that outlasts the interval leaves the retry no turn — which
    # is how a single stuck REST call ate the whole staleness budget.
    with pytest.raises(ConfigError, match="account_refresh_timeout_s"):
        Config.from_dict(
            _cfg(staleness={"account_refresh_s": 2.0, "account_refresh_timeout_s": 2.0})
        )


def test_rejects_an_evict_threshold_at_or_below_the_refresh_interval():
    """Below it, a REAL position ages out between two seeds and gets evicted —
    under-counting open spreads and releasing a concurrent slot. That is the
    direction that fails OPEN, so the relation is enforced, not documented.
    → docs/decisions/2026-08-19-account-cache-ghost-positions.md"""
    with pytest.raises(ConfigError, match="account_position_evict_after_s"):
        Config.from_dict(
            _cfg(staleness={"account_refresh_s": 2.0,
                            "account_position_evict_after_s": 2.0})
        )


def test_rejects_non_bool_trip_flag():
    with pytest.raises(ConfigError):
        Config.from_dict(_cfg(heartbeat={"trip_on_sustained_stall": "yes"}))


def test_loads_server_bind_address():
    cfg = Config.from_dict(_cfg(server={"bind_host": "192.0.2.12", "bind_port": 9000}))
    assert cfg.server.bind_host == "192.0.2.12"
    assert cfg.server.bind_port == 9000


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "", "  "])
def test_rejects_wildcard_server_bind_host(wildcard):
    # Fail closed: a wildcard bind would expose the halt control to the internet.
    with pytest.raises(ConfigError, match="bind_host"):
        Config.from_dict(_cfg(server={"bind_host": wildcard}))


@pytest.mark.parametrize("bad_port", [-1, 70000])
def test_rejects_out_of_range_server_port(bad_port):
    with pytest.raises(ConfigError, match="bind_port"):
        Config.from_dict(_cfg(server={"bind_port": bad_port}))


def test_rejects_missing_server_section():
    raw = _cfg()
    del raw["server"]
    with pytest.raises(ConfigError, match="server"):
        Config.from_dict(raw)


def test_rejects_nonpositive_heartbeat_interval():
    with pytest.raises(ConfigError, match="interval_s"):
        Config.from_dict(_cfg(heartbeat={"interval_s": 0}))


def test_loads_signal_section():
    cfg = Config.from_dict(_cfg())
    assert cfg.signal.sides == ("put",)
    assert cfg.signal.target_short_delta == 0.15
    assert cfg.signal.entry_earliest.hour == 10


def test_rejects_bad_signal_side():
    with pytest.raises(ConfigError, match="sides"):
        Config.from_dict(_cfg(signal={"sides": ["put", "straddle"]}))


def test_rejects_out_of_range_delta():
    with pytest.raises(ConfigError, match="target_short_delta"):
        Config.from_dict(_cfg(signal={"target_short_delta": 1.5}))


def test_short_delta_source_is_required():
    """It changes which strike is traded, so it has no default: a config that
    forgot it must not silently select on either source."""
    raw = _cfg()
    del raw["signal"]["short_delta_source"]
    with pytest.raises(ConfigError, match="short_delta_source"):
        Config.from_dict(raw)


def test_profit_target_enabled_is_required():
    """(2026-09-23) It switches a live behaviour on, so — unlike
    `unmanaged_order_cancel` — it has no loader default: every profile states it."""
    raw = _cfg()
    del raw["watchdog"]["profit_target_enabled"]
    with pytest.raises(ConfigError, match="profit_target_enabled"):
        Config.from_dict(raw)


@pytest.mark.parametrize("value", [1, 0, "false", "true", None])
def test_profit_target_enabled_is_strictly_a_bool(value):
    """`"false"` is truthy and `1` is an int: either would switch the take on (or off)
    by accident, so both are refused at boot rather than interpreted."""
    with pytest.raises(ConfigError, match="profit_target_enabled"):
        Config.from_dict(_cfg(watchdog={"profit_target_enabled": value}))


@pytest.mark.parametrize("value", [True, False])
def test_profit_target_enabled_loads_both_ways(value):
    cfg = Config.from_dict(_cfg(watchdog={"profit_target_enabled": value}))
    assert cfg.watchdog.profit_target_enabled is value


@pytest.mark.parametrize("value", ["MID", "implied", "", "auto"])
def test_short_delta_source_is_enum_checked(value):
    with pytest.raises(ConfigError, match="short_delta_source"):
        Config.from_dict(_cfg(signal={"short_delta_source": value}))


@pytest.mark.parametrize("value", ["mid", "feed"])
def test_short_delta_source_loads_both_modes(value):
    cfg = Config.from_dict(_cfg(signal={"short_delta_source": value}))
    assert cfg.signal.short_delta_source == value


def test_rejects_max_width_below_width():
    with pytest.raises(ConfigError, match="max_width"):
        Config.from_dict(_cfg(signal={"width": 10.0, "max_width": 5.0}))


def test_loads_chain_symbols_separately():
    # 0DTE lives under the SPXW root; spot comes from the SPX index.
    cfg = Config.from_dict(_cfg())
    assert cfg.chain.chain_symbol == "SPXW"
    assert cfg.chain.underlying_symbol == "SPX"


def test_rejects_bad_entry_time():
    with pytest.raises(ConfigError, match="entry_earliest"):
        Config.from_dict(_cfg(signal={"entry_earliest": "25:00"}))


def test_rejects_inverted_entry_window():
    with pytest.raises(ConfigError, match="before"):
        Config.from_dict(_cfg(signal={"entry_earliest": "15:00", "entry_latest": "10:00"}))


# -- [execution]: order-construction bounds ------------------------------


def test_loads_execution_section():
    assert Config.from_dict(_cfg()).execution.signal_max_age_s == 1.0


def test_execution_section_is_required():
    raw = _cfg()
    del raw["execution"]
    with pytest.raises(ConfigError, match=r"\[execution\]"):
        Config.from_dict(raw)


def test_rejects_nonpositive_signal_max_age():
    with pytest.raises(ConfigError, match="signal_max_age_s"):
        Config.from_dict(_cfg(execution={"signal_max_age_s": 0}))


def test_rejects_signal_max_age_looser_than_market_data_staleness():
    """A construction bound above the feed's own threshold can never fire — the
    pre-trade gate blocks on the looser one first. Refuse rather than ship a
    freshness check that silently does nothing."""
    with pytest.raises(ConfigError, match="signal_max_age_s"):
        Config.from_dict(_cfg(execution={"signal_max_age_s": 5.0}))  # md max age is 2.0


# -- [execution]: price-walking parameters (spec §7) ---------------------


def test_loads_price_walking_parameters():
    execution = Config.from_dict(_cfg()).execution
    assert execution.entry_cadence_s == 3.0
    assert execution.entry_step_ticks == 1
    assert execution.entry_walk_away_pct == 0.20
    assert execution.max_order_requests_per_second == 40.0


@pytest.mark.parametrize(
    "key",
    [
        "entry_cadence_s",
        "entry_step_ticks",
        "entry_walk_away_pct",
        "entry_drift_abort_points",
        "walk_source",
        "working_timeout_s",
        "status_poll_interval_s",
        "max_recovery_attempts",
        "max_order_requests_per_second",
    ],
)
def test_every_walk_parameter_is_required(key):
    """Fail closed, like everything else here. A default cadence or walk-away
    threshold is a trading decision nobody made."""
    raw = _cfg()
    del raw["execution"][key]
    with pytest.raises(ConfigError, match=key):
        Config.from_dict(raw)


@pytest.mark.parametrize("pct", [0, -0.1, 1.5])
def test_rejects_a_walk_away_pct_outside_zero_to_one(pct):
    with pytest.raises(ConfigError, match="entry_walk_away_pct"):
        Config.from_dict(_cfg(execution={"entry_walk_away_pct": pct}))


def test_rejects_a_blank_walk_provenance_note():
    """Same rule as coa.source. These numbers give away real credit on a live
    entry; an empty note means nobody has said where they came from, and a
    placeholder that looks decided is worse than none."""
    with pytest.raises(ConfigError, match="walk_source"):
        Config.from_dict(_cfg(execution={"walk_source": "   "}))


def test_rejects_a_poll_interval_slower_than_the_timeout_it_measures():
    """One status check before cancelling makes the timeout meaningless."""
    with pytest.raises(ConfigError, match="status_poll_interval_s"):
        Config.from_dict(
            _cfg(execution={"status_poll_interval_s": 30.0, "working_timeout_s": 10.0})
        )


def test_loads_shipped_sample_config():
    # The committed sandbox config must itself be valid.
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config.load(repo_root / "argus.sandbox.toml")
    assert cfg.environment in ("shadow", "sandbox", "live")


# -- staleness: the greeks bound is its own key ---------------------------
#
# 2026-08-24 split which FIELDS a leg must clear for which ROLE and kept one
# threshold. 2026-08-27 split the threshold too: a quote is a PRICE and a delta is
# a SELECTOR, and holding the second to the first's bound starved short-strike
# selection to 2.8% of ticks against a greeks feed that bursts ~137s apart.


def test_greeks_max_age_is_required():
    """No default, for the same reason `DXLinkChainSource` takes no default: a
    defaulted bound is how one call site silently keeps the old behaviour."""
    raw = _cfg()
    del raw["staleness"]["greeks_max_age_s"]
    with pytest.raises(ConfigError, match="greeks_max_age_s"):
        Config.from_dict(raw)


def test_greeks_bound_is_separate_from_the_quote_bound():
    cfg = Config.from_dict(_cfg())
    assert cfg.staleness.market_data_max_age_s == 2.0
    assert cfg.staleness.greeks_max_age_s == 8.0


def test_greeks_bound_may_not_be_tighter_than_the_quote_bound():
    """A leg is admitted on its QUOTE and only then considered as a short strike,
    so a tighter greeks bound refuses nothing the quote bound has not refused
    already — it would be `market_data_max_age_s` under a second name, undoing the
    role split by config rather than by code."""
    with pytest.raises(ConfigError, match="greeks_max_age_s"):
        Config.from_dict(_cfg(staleness={"greeks_max_age_s": 1.0}))


def test_greeks_bound_may_equal_the_quote_bound():
    # The pre-2026-08-27 posture stays expressible: this is a tuning knob, not a
    # one-way door, and a config that wants them equal must still load.
    cfg = Config.from_dict(_cfg(staleness={"greeks_max_age_s": 2.0}))
    assert cfg.staleness.greeks_max_age_s == 2.0


def test_live_does_not_restrict_the_greeks_bound():
    """Deliberately UNguarded, unlike `account_stream_max_age_s`.

    `[marketdata] source` is already "live", so the burst cadence this bound is
    sized against is the production feed's own behaviour — there is no cert
    artifact here to leave behind at the live flip.
    """
    cfg = Config.from_dict(
        _cfg(
            environment="live",
            staleness={"greeks_max_age_s": 120.0, "account_stream_max_age_s": 30.0},
            audit={"synchronous": "FULL"},
            broker={"account_number": "5WX00000"},
        )
    )
    assert cfg.staleness.greeks_max_age_s == 120.0


# -- staleness.account_stream_max_age_s: a cert-only widening, fenced -----


def test_live_refuses_a_widened_account_stream_bound():
    """The cert value must not ride into live by being forgotten.

    120s exists only to tolerate the tastytrade SANDBOX's /positions latency (245
    refresh failures in one session, 2026-08-27). In live this rail is what stops
    an entry being sized and gated against an account picture nothing has proved
    is current — so the refusal is structural, exactly like the `audit.synchronous`
    one below, rather than a comment in the config file.
    """
    with pytest.raises(ConfigError, match="account_stream_max_age_s"):
        Config.from_dict(
            _cfg(
                environment="live",
                staleness={"account_stream_max_age_s": 120.0},
                audit={"synchronous": "FULL"},
                # Pinned, so the broker guard is satisfied and THIS guard is the
                # one firing.
                broker={"account_number": "5WX00000"},
            )
        )


@pytest.mark.parametrize("environment", ["shadow", "sandbox"])
def test_non_live_allows_the_widened_account_stream_bound(environment):
    cfg = Config.from_dict(
        _cfg(environment=environment, staleness={"account_stream_max_age_s": 120.0})
    )
    assert cfg.staleness.account_stream_max_age_s == 120.0


def test_live_allows_the_account_stream_bound_at_the_ceiling():
    # Boundary, not just the refusal: `<=` is the rule, and an off-by-one here
    # would either forbid the documented live value or admit one past it.
    cfg = Config.from_dict(
        _cfg(
            environment="live",
            staleness={"account_stream_max_age_s": 30.0},
            audit={"synchronous": "FULL"},
            broker={"account_number": "5WX00000"},
        )
    )
    assert cfg.staleness.account_stream_max_age_s == 30.0


# -- audit.synchronous: durability posture as config, not a literal -------


def test_synchronous_is_required():
    raw = _cfg()
    del raw["audit"]["synchronous"]
    with pytest.raises(ConfigError, match="audit.synchronous"):
        Config.from_dict(raw)


@pytest.mark.parametrize("value", ["OFF", "NORMAL", "FULL", "EXTRA"])
def test_synchronous_accepts_the_sqlite_values(value):
    assert Config.from_dict(_cfg(audit={"synchronous": value})).audit.synchronous == value


def test_synchronous_is_normalised_to_upper():
    cfg = Config.from_dict(_cfg(audit={"synchronous": "full"}))
    assert cfg.audit.synchronous == "FULL"


def test_synchronous_rejects_nonsense():
    with pytest.raises(ConfigError, match="audit.synchronous"):
        Config.from_dict(_cfg(audit={"synchronous": "SOMETIMES"}))


@pytest.mark.parametrize("value", ["OFF", "NORMAL"])
def test_live_refuses_a_non_durable_synchronous(value):
    """Live must not inherit shadow's weaker durability by omission.

    Under NORMAL a host crash can roll back the last commits — and the audit DB
    also holds the `positions` table reconciliation compares the broker against.
    """
    with pytest.raises(ConfigError, match="requires audit.synchronous"):
        Config.from_dict(
            _cfg(
                environment="live",
                audit={"synchronous": value},
                # Pinned, so the broker guard is satisfied and THIS guard is the
                # one firing.
                broker={"account_number": "5WX00000"},
            )
        )


@pytest.mark.parametrize("value", ["FULL", "EXTRA"])
def test_live_accepts_a_durable_synchronous(value):
    cfg = Config.from_dict(
        _cfg(
            environment="live",
            audit={"synchronous": value},
            broker={"account_number": "5WX00000"},
        )
    )
    assert cfg.audit.synchronous == value


@pytest.mark.parametrize("env", ["shadow", "sandbox"])
def test_non_live_may_use_normal(env):
    cfg = Config.from_dict(_cfg(environment=env, audit={"synchronous": "NORMAL"}))
    assert cfg.audit.synchronous == "NORMAL"


def test_live_triggers_the_durability_rule():
    with pytest.raises(ConfigError, match="requires audit.synchronous"):
        Config.from_dict(
            _cfg(
                environment="live",
                audit={"synchronous": "NORMAL"},
                staleness={"account_stream_max_age_s": 30.0},
                broker={"account_number": "5WX00000"},
            )
        )


# -- broker.http_timeout_s: an explicit bound, not httpx's inherited 5s ----


def test_http_timeout_is_required():
    raw = _cfg()
    del raw["broker"]["http_timeout_s"]
    with pytest.raises(ConfigError, match="broker.http_timeout_s"):
        Config.from_dict(raw)


def test_http_timeout_must_be_positive():
    with pytest.raises(ConfigError, match="broker.http_timeout_s"):
        Config.from_dict(_cfg(broker={"http_timeout_s": 0}))


def test_broker_section_is_required():
    raw = _cfg()
    del raw["broker"]
    with pytest.raises(ConfigError, match=r"\[broker\]"):
        Config.from_dict(raw)


def test_http_timeout_loads():
    assert Config.from_dict(_cfg(broker={"http_timeout_s": 45.0})).broker.http_timeout_s == 45.0


# -- broker.account_number: live must NAME its account -----------------------
#
# The enforcement half of C9 (2026-09-10): every account-derived rail reads a
# WHOLE-ACCOUNT number and treats it as an Argus-only number, so the live account
# must be dedicated to Argus AND named — a dedicated account that is merely
# assumed is one re-ordered `GET /customers/me/accounts` away from not being the
# one. → docs/decisions/2026-08-31-live-account-is-not-dedicated.md
#
# All live constructions here pin the deliberately fake 5WX00000 — never the real
# number, so no test can be mistaken for production.


def test_live_requires_a_pinned_account_number():
    """The key absent entirely. The other two live guards are satisfied, so it is
    THIS refusal firing and not one of theirs."""
    raw = _cfg(
        environment="live",
        staleness={"account_stream_max_age_s": 30.0},
        audit={"synchronous": "FULL"},
    )
    del raw["broker"]["account_number"]
    with pytest.raises(ConfigError, match="broker.account_number"):
        Config.from_dict(raw)


@pytest.mark.parametrize("empty", ["", "   ", "\t"])
def test_live_refuses_an_empty_account_number(empty):
    """The state dev and sandbox ship, so it is the one that must not ride into
    live by being copied. Whitespace-only counts: it normalizes to None."""
    with pytest.raises(ConfigError, match="broker.account_number"):
        Config.from_dict(
            _cfg(
                environment="live",
                broker={"account_number": empty},
                staleness={"account_stream_max_age_s": 30.0},
                audit={"synchronous": "FULL"},
            )
        )


def test_live_accepts_a_pinned_account_number():
    cfg = Config.from_dict(
        _cfg(
            environment="live",
            broker={"account_number": "5WX00000"},
            staleness={"account_stream_max_age_s": 30.0},
            audit={"synchronous": "FULL"},
        )
    )
    assert cfg.broker.account_number == "5WX00000"


def test_an_account_number_is_stripped():
    """Trailing whitespace in a hand-edited TOML string is invisible and would
    otherwise match nothing at the broker — the account filter answers a no-match
    with a PERMANENT SessionAborted."""
    cfg = Config.from_dict(_cfg(broker={"account_number": "  5WX00000  "}))
    assert cfg.broker.account_number == "5WX00000"


def test_an_account_number_with_internal_whitespace_is_refused():
    """A `SessionAborted` park is a worse failure than a failed start."""
    with pytest.raises(ConfigError, match="must not contain whitespace"):
        Config.from_dict(_cfg(broker={"account_number": "5WX 00000"}))


def test_an_account_number_must_be_a_string():
    """`account_number = 5` is the natural TOML typo, and the account filter
    cannot catch it — it never matches, and parks the process."""
    with pytest.raises(ConfigError, match="must be a string") as exc:
        Config.from_dict(_cfg(broker={"account_number": 5}))
    assert "int" in str(exc.value)


@pytest.mark.parametrize("env", ["shadow", "sandbox"])
def test_non_live_loads_unpinned(env):
    """Both the shipped `""` and the key's absence, in both cert-side
    environments. `None` is unpinned = accounts[0], unchanged since before the
    key existed."""
    with_value = Config.from_dict(_cfg(environment=env, broker={"account_number": ""}))
    raw = _cfg(environment=env)
    del raw["broker"]["account_number"]
    without_key = Config.from_dict(raw)
    assert with_value.broker.account_number is None
    assert without_key.broker.account_number is None


def test_the_three_live_guards_are_independent():
    """Each fires with the other two satisfied, each with its own sentence.

    This is the test that fails if someone ever folds the three live refusals
    into one branch — the mistake `account_stream_max_age_s`'s own config comment
    warns against: only one of the three is a cert artifact.
    """
    # 1. broker.account_number, with staleness and audit satisfied.
    with pytest.raises(ConfigError, match="broker.account_number"):
        Config.from_dict(
            _cfg(
                environment="live",
                staleness={"account_stream_max_age_s": 30.0},
                audit={"synchronous": "FULL"},
            )
        )
    # 2. staleness.account_stream_max_age_s, with the account and audit satisfied.
    with pytest.raises(ConfigError, match="account_stream_max_age_s"):
        Config.from_dict(
            _cfg(
                environment="live",
                broker={"account_number": "5WX00000"},
                staleness={"account_stream_max_age_s": 120.0},
                audit={"synchronous": "FULL"},
            )
        )
    # 3. audit.synchronous, with the account and staleness satisfied.
    with pytest.raises(ConfigError, match="requires audit.synchronous"):
        Config.from_dict(
            _cfg(
                environment="live",
                broker={"account_number": "5WX00000"},
                staleness={"account_stream_max_age_s": 30.0},
                audit={"synchronous": "NORMAL"},
            )
        )


# -- [market]: scheduling calendar + session-open login retry ----------------


def test_market_section_is_required():
    raw = _cfg()
    del raw["market"]
    with pytest.raises(ConfigError, match=r"\[market\]"):
        Config.from_dict(raw)


def test_market_loads_times_and_dates():
    from datetime import date, time

    cfg = Config.from_dict(_cfg())
    assert cfg.market.session_open == time(9, 40)
    assert cfg.market.half_day_close == time(13, 0)
    assert date(2026, 11, 26) in cfg.market.holidays
    assert date(2026, 11, 27) in cfg.market.half_days


def test_market_rejects_open_at_or_after_close():
    with pytest.raises(ConfigError, match="session_open must be before"):
        Config.from_dict(_cfg(market={"session_open": "16:00", "session_close": "16:00"}))


def test_market_rejects_open_at_or_after_half_day_close():
    with pytest.raises(ConfigError, match="half_day_close"):
        Config.from_dict(_cfg(market={"session_open": "13:30"}))


def test_market_rejects_a_malformed_holiday_date():
    with pytest.raises(ConfigError, match="ISO date"):
        Config.from_dict(_cfg(market={"holidays": ["2026-13-01"]}))


def test_market_requires_the_holiday_lists():
    raw = _cfg()
    del raw["market"]["holidays"]
    with pytest.raises(ConfigError, match="market.holidays"):
        Config.from_dict(raw)


def test_market_login_retry_must_be_positive():
    with pytest.raises(ConfigError, match="market.login_attempts"):
        Config.from_dict(_cfg(market={"login_attempts": 0}))


# -- [watchdog] (chunk 4) -----------------------------------------------------


def test_loads_watchdog_section():
    wd = Config.from_dict(_cfg()).watchdog
    assert wd.credit_multiple == 2.0
    assert wd.underlying_buffer_points == 5.0
    assert wd.sustained_s == 2.0
    assert wd.exit_cadence_s == 1.5
    assert wd.exit_step_ticks == 1
    assert wd.exit_walk_away_pct == 0.20
    assert wd.exit_walk_min_steps == 1
    assert wd.exit_cancel_settle_s == 2.0
    assert wd.exit_unattempted_max_s == 20.0
    assert wd.book_settle_s == 10.0
    assert wd.book_confirm_ttl_s == 30.0
    assert wd.trigger_source == "test"


def test_watchdog_section_is_required():
    raw = _cfg()
    del raw["watchdog"]
    with pytest.raises(ConfigError, match=r"\[watchdog\]"):
        Config.from_dict(raw)


@pytest.mark.parametrize(
    "key",
    [
        "cadence_s",
        "refresh_interval_s",
        "book_settle_s",
        "book_confirm_ttl_s",
        "credit_multiple",
        "underlying_buffer_points",
        "sustained_s",
        "exit_cadence_s",
        "exit_step_ticks",
        "exit_walk_away_pct",
        "exit_walk_min_steps",
        "exit_cancel_settle_s",
        "exit_unattempted_max_s",
        "trigger_source",
    ],
)
def test_every_watchdog_key_is_required(key: str):
    raw = _cfg()
    del raw["watchdog"][key]
    with pytest.raises(ConfigError, match=key):
        Config.from_dict(raw)


@pytest.mark.parametrize("bound", [0.0, -1.0])
def test_the_exit_silence_bound_has_no_off_switch(bound: float):
    """Unlike book_settle_s, 0 is NOT an escape hatch here: it would page on the
    tick after the rising edge, i.e. twice for one condition. An exit that can
    never be built is never a condition to stop reporting."""
    with pytest.raises(ConfigError, match="exit_unattempted_max_s"):
        Config.from_dict(_cfg(watchdog={"exit_unattempted_max_s": bound}))


@pytest.mark.parametrize("ttl", [5.0, 4.0, 0.5])
def test_rejects_a_book_confirm_ttl_at_or_below_the_refresh_cadence(ttl: float):
    """`refresh_interval_s` is 5.0 in the test config, and the book confirm is only
    consulted from `_refresh_if_due`. A TTL at or under that cadence expires every
    verdict before it can be reused, restoring a REST round trip — and the blind
    window that comes with it — on every observation."""
    with pytest.raises(ConfigError, match="book_confirm_ttl_s"):
        Config.from_dict(_cfg(watchdog={"book_confirm_ttl_s": ttl}))


def test_a_book_confirm_ttl_just_above_the_cadence_is_accepted():
    """The bound is the cadence itself, not a multiple of it — the loader refuses
    the configuration that cannot work, and leaves the sizing to the operator."""
    cfg = Config.from_dict(_cfg(watchdog={"book_confirm_ttl_s": 5.5}))
    assert cfg.watchdog.book_confirm_ttl_s == 5.5


@pytest.mark.parametrize("multiple", [1.0, 0.5, 0.0, -2.0])
def test_rejects_a_credit_multiple_at_or_below_one(multiple: float):
    """At 1.0 the cost to close equals the credit received, so the trigger fires at
    break-even — an immediate exit, not a stop."""
    with pytest.raises(ConfigError, match="credit_multiple"):
        Config.from_dict(_cfg(watchdog={"credit_multiple": multiple}))


def test_a_zero_underlying_buffer_is_valid():
    """0 means "trigger when the short strike is touched", which is a real choice."""
    cfg = Config.from_dict(_cfg(watchdog={"underlying_buffer_points": 0.0}))
    assert cfg.watchdog.underlying_buffer_points == 0.0


def test_rejects_a_negative_underlying_buffer():
    """Negative would mean waiting until the strike is already breached by a margin."""
    with pytest.raises(ConfigError, match="underlying_buffer_points"):
        Config.from_dict(_cfg(watchdog={"underlying_buffer_points": -1.0}))


def test_rejects_a_debounce_shorter_than_the_cadence():
    """A single observation would satisfy it, so the debounce would do nothing and a
    momentary spread blowout would trigger an exit (spec §7)."""
    with pytest.raises(ConfigError, match="sustained_s"):
        Config.from_dict(_cfg(watchdog={"cadence_s": 2.0, "sustained_s": 1.0}))


def test_a_debounce_equal_to_the_cadence_is_allowed():
    cfg = Config.from_dict(_cfg(watchdog={"cadence_s": 1.0, "sustained_s": 1.0}))
    assert cfg.watchdog.sustained_s == 1.0


def test_rejects_a_ledger_refresh_faster_than_the_cadence():
    """The ledger read shares the audit log's writer lock with crash-critical Tier 1
    writes; `AuditLog.read`'s contract is that reads are rare."""
    with pytest.raises(ConfigError, match="refresh_interval_s"):
        Config.from_dict(_cfg(watchdog={"cadence_s": 1.0, "refresh_interval_s": 0.5}))


@pytest.mark.parametrize("pct", [0.0, -0.1, 1.5])
def test_rejects_an_exit_walk_away_pct_outside_zero_to_one(pct: float):
    with pytest.raises(ConfigError, match="exit_walk_away_pct"):
        Config.from_dict(_cfg(watchdog={"exit_walk_away_pct": pct}))


@pytest.mark.parametrize("steps", [0, -1])
def test_the_exit_walk_minimum_has_no_off_switch(steps: int):
    """0 is exactly the behaviour this key exists to end (2026-08-31): a walk with no
    reprice in it offers the mid once and escalates straight to the unguarded market
    cross, which in the flatten's modal regime was the whole of both exits."""
    with pytest.raises(ConfigError, match="exit_walk_min_steps"):
        Config.from_dict(_cfg(watchdog={"exit_walk_min_steps": steps}))


def test_a_zero_book_settle_is_valid():
    """0 means "page on the first observation" — the pre-2026-08-13 behaviour, and a
    real choice an operator may want back without a code change."""
    cfg = Config.from_dict(_cfg(watchdog={"book_settle_s": 0.0}))
    assert cfg.watchdog.book_settle_s == 0.0


def test_rejects_a_negative_book_settle():
    with pytest.raises(ConfigError, match="book_settle_s"):
        Config.from_dict(_cfg(watchdog={"book_settle_s": -1.0}))


def test_requires_a_trigger_provenance_note():
    """Same rule as coa.source and execution.walk_source: these numbers decide when a
    live losing position is closed, so "nobody has said where they came from" must be
    a load failure rather than a silent default."""
    with pytest.raises(ConfigError, match="trigger_source"):
        Config.from_dict(_cfg(watchdog={"trigger_source": "   "}))


# -- the absolute size rails and the dry-run switch (chunk 5) ------------------


def test_requires_the_absolute_size_rails():
    """Fail-closed, like every other risk value: a missing cap is not an absent limit."""
    for key in ("max_loss_per_position", "max_contracts_per_position"):
        raw = _cfg()
        del raw["risk"][key]
        with pytest.raises(ConfigError, match=key):
            Config.from_dict(raw)


def test_rejects_a_per_position_cap_above_the_daily_cap():
    """A per-trade cap above the day's cap can never bind before the breaker does,
    which makes it decoration rather than a rail."""
    with pytest.raises(ConfigError, match="max_loss_per_position"):
        Config.from_dict(_cfg(risk={"daily_max_loss": 1000.0, "max_loss_per_position": 1001.0}))


def test_a_per_position_cap_equal_to_the_daily_cap_is_allowed():
    cfg = Config.from_dict(_cfg(risk={"daily_max_loss": 1000.0, "max_loss_per_position": 1000.0}))
    assert cfg.risk.max_loss_per_position == 1000.0


def test_dry_run_submissions_defaults_to_TRUE_when_absent():
    """The ONE defaulting key in the loader, and it defaults to the safe side: a config
    predating chunk 5 must not start routing real orders the moment `environment` is
    flipped to sandbox. Fail-closed for an order-routing switch means "do not route"."""
    raw = _cfg()
    del raw["execution"]["dry_run_submissions"]
    assert Config.from_dict(raw).execution.dry_run_submissions is True


def test_dry_run_submissions_must_be_a_real_bool():
    """`"false"` is truthy in Python, so a quoted value would silently mean the
    opposite of what its author wrote."""
    with pytest.raises(ConfigError, match="dry_run_submissions"):
        Config.from_dict(_cfg(execution={"dry_run_submissions": "false"}))


# -- [gc]: optional, defaulted, never a reason a trading day cannot start ------


def test_gc_section_is_optional_and_defaults_are_the_shipped_tuning():
    """A NEW REQUIRED key would refuse to start against a pre-existing config —
    the deploy trap [watchdog] and [risk] already document. GC tuning is a
    performance knob; it must never block the open."""
    raw = copy.deepcopy(CONFIG)
    raw.pop("gc", None)
    cfg = Config.from_dict(raw)
    assert cfg.gc == GcConfig()
    assert cfg.gc.freeze_on_startup is True
    assert cfg.gc.gen2_threshold == 100


def test_gc_tuning_can_be_disabled_and_bad_values_are_refused():
    raw = copy.deepcopy(CONFIG)
    raw["gc"] = {"freeze_on_startup": False, "gen2_threshold": 10}
    cfg = Config.from_dict(raw)
    assert cfg.gc.freeze_on_startup is False and cfg.gc.gen2_threshold == 10

    # A present value must be a REAL bool — "false" is a ConfigError, not a truthy
    # string. Same rule execution.dry_run_submissions follows: a typo'd toggle that
    # silently reads as "on" is the failure mode.
    for bad in ({"freeze_on_startup": "false"}, {"gen2_threshold": 0},
                {"gen2_threshold": "100"}, {"gen2_threshold": True}):
        broken = copy.deepcopy(CONFIG)
        broken["gc"] = bad
        with pytest.raises(ConfigError):
            Config.from_dict(broken)
