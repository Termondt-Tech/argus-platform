"""Shared test fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from argus.audit.log import AuditLog
from argus.control import KILL_TOKEN_VAR
from argus.db import apply_migrations, connect


@pytest.fixture(autouse=True)
def _kill_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a kill-switch bearer token in the environment.

    `Application.startup()` now resolves ARGUS_KILL_TOKEN and refuses to start
    without it (an unauthenticated halt control is a broken safety rail). Set a
    default here so the many startup-based tests don't each have to; tests that
    exercise the missing-token path delete it explicitly.
    """
    monkeypatch.setenv(KILL_TOKEN_VAR, "test-kill-token")


#: Every variable that names a REAL outbound channel. A test that needs one sets it
#: itself, to a fake, after this fixture has run.
_OUTBOUND_ENV_VARS = ("ARGUS_SLACK_WEBHOOK_URL", "ARGUS_REPORT_SLACK_WEBHOOK_URL")


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must never see the developer's real `.env`.

    `bootstrap.load_env()` writes the repo's `.env` into `os.environ` with a plain
    `load_dotenv` — NOT through monkeypatch — so the first test that drives a CLI
    `main()` leaks every real credential into the rest of the pytest process. On
    2026-09-18 that was the real alert webhook, and every startup-based test after
    `tests/test_eod_report.py` posted to Slack. Blinding `find_dotenv` makes
    `load_env()` a no-op at every call site, including modules that imported the
    name directly; clearing the webhook variables covers one exported in the shell.
    """
    monkeypatch.setattr("argus.bootstrap.find_dotenv", lambda *a, **k: "")
    for var in _OUTBOUND_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "test.db")
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def audit(db: sqlite3.Connection) -> AuditLog:
    # Small queue so the "queue full" path is cheap to exercise.
    return AuditLog(db, tier2_queue_maxsize=8, tier2_queue_alert_depth=6)
