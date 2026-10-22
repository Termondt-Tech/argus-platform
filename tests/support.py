"""Shared test configuration.

ONE canonical valid config dict for the whole suite. Previously every test module
kept its own copy, so each new required config section ([heartbeat], [signal],
[chain], ...) broke all of them at once. Adding a section now means editing this
file only.

These are test values, not production ones — `argus.toml` holds the real
defaults. Notably `account_stream_max_age_s` is kept tight here so staleness
tests stay meaningful.
"""

from __future__ import annotations

import copy
from typing import Any

from argus.config import Config

CONFIG: dict[str, Any] = {
    "environment": "shadow",
    # `account_number` mirrors the shipped cert files: EMPTY = unpinned = take the
    # login's first account. Deliberately not a number here — the default must stay
    # the unpinned one, or every test starts asserting against a pin nothing set
    # (2026-09-10, C9). A test that needs a pin says so locally, with the fake
    # number the design reserves for tests: 5WX00000.
    "broker": {"http_timeout_s": 30.0, "account_number": ""},
    # Tiny outer budget by default so a test that accidentally lets the real loop
    # run does not sit for 30 minutes. The reconcile tests that care override
    # these explicitly and monkeypatch `argus.reconcile.asyncio.sleep`.
    "reconcile": {
        "request_timeout_s": 10.0,
        "retry_interval_s": 0.01,
        "retry_budget_s": 0.03,
    },
    "marketdata": {"source": "sandbox"},
    "coa": {"response_window_ms": 100, "extended_processing_ms": 900, "source": "test"},
    "risk": {
        "daily_max_loss": 1000.0,
        "max_concurrent_positions": 2,
        "position_size_pct": 0.02,
        # `max_loss_per_position` is at its loosest LEGAL value here — the loader
        # refuses anything above `daily_max_loss`, and 1000.0 is load-bearing for the
        # breaker tests (test_gate trips it with a -1500 day P&L), so it cannot simply
        # be raised. Consequence worth knowing when reading a sizing assertion: on a
        # spread risking 420/contract this rail binds at 2 contracts, TIGHTER than
        # 2%-of-100k's 4. The contract cap is deliberately far out of the way so only
        # one absolute rail binds by default.
        "max_loss_per_position": 1000.0,
        "max_contracts_per_position": 100,
        # OFF by default here (production profiles ship it ON): every test that
        # predates the rule keeps exercising the pre-2026-09-26 entry path exactly,
        # and `tests/test_side_balance.py` turns it on where it is under test.
        "side_balance": False,
        "max_side_imbalance": 1,
    },
    "staleness": {
        "market_data_max_age_s": 2.0,
        # Deliberately LOOSER than the quote bound, as in production — a fixture
        # where the two are equal cannot tell a test that conflated them from one
        # that did not.
        "greeks_max_age_s": 8.0,
        "account_stream_max_age_s": 5.0,
        "account_refresh_s": 2.0,
        "account_refresh_timeout_s": 1.0,
        # Must stay > account_refresh_s: a real position is re-touched by every
        # seed, so only a ghost can ever age past this.
        "account_position_evict_after_s": 12.0,
    },
    "audit": {
        "db_path": "argus.db",
        "tier2_queue_maxsize": 100,
        "tier2_queue_alert_depth": 80,
        "synchronous": "NORMAL",
    },
    "heartbeat": {
        "interval_s": 0.1,
        "lag_alert_threshold_s": 0.05,
        "sustained_breach_count": 5,
        "trip_on_sustained_stall": False,
    },
    "signal": {
        "sides": ["put"],
        "target_short_delta": 0.15,
        "width": 5.0,
        "max_width": 10.0,
        "min_credit": 0.75,
        "entry_earliest": "10:00",
        "entry_latest": "15:00",
        "cadence_s": 5.0,
        # "feed" here, deliberately, while every PROFILE ships "mid": the shared
        # fixture chains carry hand-set feed deltas and no regime-consistent
        # prices, so the pre-existing suite keeps asserting the behaviour it was
        # written against. The mid selector has its own tests.
        "short_delta_source": "feed",
    },
    "execution": {
        # Tight, like production: the bound is gate-pass -> order construction,
        # not feed health. Must stay <= staleness.market_data_max_age_s.
        "signal_max_age_s": 1.0,
        # Price-walking loop. Test values, deliberately NOT argus.toml's: a
        # cadence above the COA settle floor so tests exercise the un-clamped
        # path, and a wide enough walk-away that a multi-step walk is expressible.
        "entry_cadence_s": 3.0,
        "entry_step_ticks": 1,
        "entry_walk_away_pct": 0.20,
        # Wide enough that the fixtures' underlying prices never trip the drift
        # breaker by accident — a test about drift arms it explicitly.
        "entry_drift_abort_points": 3.0,
        "walk_source": "test",
        "working_timeout_s": 10.0,
        "status_poll_interval_s": 0.5,
        "max_recovery_attempts": 3,
        "max_order_requests_per_second": 40.0,
        # Duplicate-guard bound and blind-idea streak. Production values — the
        # burst's own attempts and backoff are module constants, and the suite
        # drives the timing through the injected sleep rather than the clock.
        "guard_timeout_s": 3.0,
        "guard_unreachable_max_streak": 3,
        # FALSE here, unlike argus.toml's fail-closed default: the default exists so
        # a production config that predates the key cannot start routing orders, but
        # a test suite that inherited it would silently exercise the dry-run branch
        # everywhere the real submission path is what is under test. Dry-run tests
        # set it to True locally.
        "dry_run_submissions": False,
    },
    "watchdog": {
        # Test values. Cadence and debounce are kept small so a test can express a
        # multi-observation sustain without a fake clock having to count to 30, but
        # the invariants the loader enforces still hold here
        # (refresh_interval_s >= cadence_s, sustained_s >= cadence_s).
        "cadence_s": 0.5,
        "refresh_interval_s": 5.0,
        # Small enough that a fake clock can step over it in one line, non-zero so
        # the allowance path is the one under test by default.
        "book_settle_s": 10.0,
        # Comfortably above refresh_interval_s (5.0), which the loader requires, and
        # small enough that a fake clock steps over it in one line when a test is
        # about the expiry itself.
        "book_confirm_ttl_s": 30.0,
        "credit_multiple": 2.0,
        "underlying_buffer_points": 5.0,
        "sustained_s": 2.0,
        # Above the 1.0s COA settle floor, so tests exercise the un-clamped path;
        # production sits at or under it, which is the designed behaviour for an exit.
        "exit_cadence_s": 1.5,
        "exit_step_ticks": 1,
        "exit_walk_away_pct": 0.20,
        # Production's value. At 0.20 the percentage vanishes below a debit of 0.125,
        # so without this the ladder has no rungs at all in the flatten's modal
        # regime (2026-08-31).
        "exit_walk_min_steps": 1,
        "exit_cancel_settle_s": 2.0,
        # Small enough that a fake clock steps over it in one line; well above
        # cadence_s so the ordinary rising-edge page is not immediately followed by
        # the overdue one.
        "exit_unattempted_max_s": 20.0,
        # The production value, deliberately: 15:30 ET on a normal close and 12:30 on
        # a half-day, so a test that reasons about the mark reasons about the real
        # one. Comfortably inside the shortest session the test calendar can produce
        # (09:40 -> 13:00 = 12000s), which the loader enforces.
        "flatten_before_close_s": 1800.0,
        # Comfortably above refresh_interval_s (5.0), which the loader requires, and
        # small enough that a fake clock steps over it in one line when a test is
        # about the re-cancel rate or the repeating page.
        "unmanaged_order_max_s": 30.0,
        # `would_cancel` by default here for the same reason production ships that
        # way: a test that means to exercise the outbound call says so.
        "unmanaged_order_cancel": False,
        # Production's shipped value: the trigger is evaluated and recorded, and
        # nothing is sent. A test about the profit target's ACTION turns it on.
        "profit_target_enabled": False,
        # ON by default here (production ships it OFF since 2026-09-26, the
        # strike-touch stop): every pre-existing credit-trigger test keeps its
        # meaning. `tests/test_strike_touch_stop.py` turns it off.
        "credit_trigger_enabled": True,
        # Production ships TRUE in all three profiles; the suite keeps it OFF so
        # the pre-existing spent-exit tests are unedited (a stub continuation
        # would add a second executor call to every FILLED-spent fixture). A
        # test about the stub close turns it on.
        "stub_close_enabled": False,
        "trigger_source": "test",
        # OFF in the suite so every pre-existing flatten test is unedited; a test
        # about the deferral turns it on. The three numbers are production's.
        "flatten_deferral": "off",
        "flatten_defer_insurance_multiple": 10.0,
        "flatten_defer_tail_z99": 2.33,
        "flatten_defer_tail_alpha": 2.93,
        "flatten_defer_source": "test",
    },
    "chain": {
        "underlying_symbol": "SPX",
        "chain_symbol": "SPXW",
        "strike_window_points": 150.0,
    },
    "market": {
        "session_open": "09:40",
        "session_close": "16:00",
        "half_day_close": "13:00",
        # A couple of representative dates so calendar tests have something to
        # assert against; real calendars live in argus.toml.
        "holidays": ["2026-07-03", "2026-11-26", "2026-12-25"],
        "half_days": ["2026-11-27", "2026-12-24"],
        # Tight so login-retry tests don't wait; production values are minutes.
        "login_attempts": 2,
        "login_retry_interval_s": 0.01,
    },
    "server": {
        # Loopback + OS-assigned port: the control server binds on a real (but
        # private, always-present) interface in tests, and each test gets a free
        # ephemeral port with no collisions. Production sets the Tailscale IP and
        # a fixed port in argus.toml.
        "bind_host": "127.0.0.1",
        "bind_port": 0,
    },
}


def config_dict(**overrides: Any) -> dict[str, Any]:
    """A fresh copy of the canonical config. Dict overrides shallow-merge into
    their section; anything else replaces the top-level key."""
    raw = copy.deepcopy(CONFIG)
    for key, patch in overrides.items():
        if isinstance(patch, dict) and isinstance(raw.get(key), dict):
            raw[key].update(patch)
        else:
            raw[key] = patch
    return raw


def make_config(**overrides: Any) -> Config:
    return Config.from_dict(config_dict(**overrides))


def fresh_loss_budget(daily_max_loss: float = 1000.0):
    """A loss budget with nothing realized and nothing open — the whole cap left.

    For a `RiskSnapshot` whose test is not about the loss budget. Matches
    `CONFIG["risk"]["daily_max_loss"]` by default.
    """
    from argus.risk.loss_budget import LossBudget

    return LossBudget.fresh(daily_max_loss)


def loss_budget_source(daily_max_loss: float = 1000.0, *, ledger=None, live: bool = False):
    """A `LossBudgetSource` over an empty (or given) day ledger, nothing open."""
    from argus.risk.loss_budget import DayLedger, LossBudgetSource

    return LossBudgetSource(
        daily_max_loss=daily_max_loss,
        ledger=ledger if ledger is not None else DayLedger(),
        broker_pnl_is_real=live,
    )
