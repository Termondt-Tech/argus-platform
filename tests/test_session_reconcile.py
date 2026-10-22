"""Environment-gated response to a session-START reconcile mismatch.

The per-trading-day reconcile (`session._reconcile_or_abort`) is the call site
Chunk 1 introduced and this chunk hardens. Its mismatch handling is gated on
environment:

  - shadow: abort THIS day only (record verdict, alert, raise SessionAborted) and
    keep the process alive for tomorrow — the Chunk-1 behaviour, which must NOT
    tighten (nothing executes in shadow, and a hard halt would let a stale sandbox
    position wiped nightly brick every following day).
  - sandbox/live: hard-halt the whole process via the injected `halt` seam — the
    same engage-kill-switch + request-stop pairing the audit-write-failure fix
    uses — with `source="reconciliation_mismatch"` and the discrepancy detail.

Sandbox is tested EXPLICITLY, not left to inherit live's coverage. These call the
real `_reconcile_or_abort` (real Reconciler + real DB read of the empty positions
table); only the audit sink and the halt seam are stubbed.

Every config here overrides `audit.db_path` to a tmp file — the live `argus.db`
is production data and dev/test must never open it (CLAUDE.md).
"""

from __future__ import annotations

import pytest

from argus.account.fake import FakeBrokerAccount
from argus.account.models import Position
from argus.app import Application
from argus.clock import et_today
from argus.db import apply_migrations, connect
from argus.reconcile import HaltGrade, InMemoryPositionStore, Reconciler
from argus.session import SessionAborted, _reconcile_or_abort
from support import make_config

# A broker position Argus never recorded -> UNEXPECTED_AT_BROKER against the empty
# positions table -> a mismatch, in every environment.
_UNEXPECTED = Position("SPXW260718P05000000", "SPX", "Equity Option", -1, 1.20, 0.9)


class _StubAudit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    async def record(self, category, payload):
        self.records.append((category, payload))


class _HaltSpy:
    """Stands in for `Application._halt_process` — records how it was called."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, reason, *, source, grade=HaltGrade.ALL, detail=None):
        self.calls.append(
            {"reason": reason, "source": source, "grade": grade, "detail": detail}
        )


def _config(tmp_path, environment):
    audit = {"db_path": str(tmp_path / "argus.db")}
    overrides = {}
    if environment == "live":
        # config.py refuses environment='live' with a non-durable synchronous and
        # without a named account — satisfy both, so a test's OWN subject is the
        # only thing that can fail it.
        audit["synchronous"] = "FULL"
        overrides["broker"] = {"account_number": "5WX00000"}
    return make_config(environment=environment, audit=audit, **overrides)


def _prepare_db(path):
    """Create the tmp DB with the positions table (empty).

    `_reconcile_or_abort` opens its OWN connection to db_path and does NOT migrate
    (the supervisor already did under the writer lock), so the table must already
    exist. Empty = no expected positions, so a broker holding anything mismatches.
    """
    conn = connect(path)
    apply_migrations(conn)
    conn.close()


async def test_shadow_mismatch_aborts_day_without_halting(tmp_path):
    """Regression: shadow behaviour is unchanged — abort the day, do NOT halt."""
    cfg = _config(tmp_path, "shadow")
    _prepare_db(cfg.audit.db_path)
    audit, halt, alerts = _StubAudit(), _HaltSpy(), []

    with pytest.raises(SessionAborted):
        await _reconcile_or_abort(
            FakeBrokerAccount([_UNEXPECTED]), cfg, audit,
            lambda k, d: alerts.append(k), halt,
        )

    assert halt.calls == []                     # process NOT halted
    assert alerts == ["reconciliation_mismatch"]  # still alerts
    # The verdict is on the record (ok=False), same as before.
    assert ("reconcile.result", {"ok": False, "discrepancies": [
        {"symbol": _UNEXPECTED.symbol, "kind": "unexpected_at_broker",
         "expected_qty": 0, "broker_qty": -1},
    ], "order_discrepancies": [], "trading_date": et_today().isoformat()}) in audit.records


@pytest.mark.parametrize("environment", ["sandbox", "live"])
async def test_nonshadow_mismatch_halts_the_process(tmp_path, environment):
    """Sandbox AND live hard-halt: engage + stop via `halt`, attributed with detail."""
    cfg = _config(tmp_path, environment)
    _prepare_db(cfg.audit.db_path)
    audit, halt, alerts = _StubAudit(), _HaltSpy(), []

    # It still raises SessionAborted (unwinds this session cleanly); the difference
    # from shadow is that it FIRST halts the whole process.
    with pytest.raises(SessionAborted):
        await _reconcile_or_abort(
            FakeBrokerAccount([_UNEXPECTED]), cfg, audit,
            lambda k, d: alerts.append(k), halt,
        )

    assert len(halt.calls) == 1
    call = halt.calls[0]
    assert call["source"] == "reconciliation_mismatch"
    # Enough detail in the trip to tell the whole story without re-deriving it.
    assert call["detail"]["ok"] is False
    assert call["detail"]["discrepancies"][0]["symbol"] == _UNEXPECTED.symbol
    assert call["detail"]["discrepancies"][0]["kind"] == "unexpected_at_broker"
    assert alerts == ["reconciliation_mismatch"]


async def test_nonshadow_without_halt_seam_fails_loud(tmp_path):
    """A sandbox/live body wired with no halt seam is a broken rail — refuse, don't
    silently run past the mismatch."""
    cfg = _config(tmp_path, "sandbox")
    _prepare_db(cfg.audit.db_path)

    with pytest.raises(AssertionError, match="halt seam"):
        await _reconcile_or_abort(
            FakeBrokerAccount([_UNEXPECTED]), cfg, _StubAudit(),
            lambda *_: None, halt=None,
        )


async def test_nonshadow_halt_writes_detail_durably_before_stop(tmp_path):
    """Q2 ordering guarantee, through the REAL halt path (not a spy): the mismatch
    detail is DURABLE in the audit log before the process is even asked to stop —
    so although the hard-halt takes /status down with the whole Application, the
    discrepancy stays retrievable by a direct query over SSH.

    `_halt_process` awaits the Tier-1 trip write, THEN calls request_stop() (which
    only sets an Event — it does not force an exit), so by the time stop is set the
    trip is on disk. And `reconcile.result` was written earlier still, independently
    of the trip, so the detail survives even if the trip write were to fail. This is
    the same ordering /kill's 200-after-write path already guarantees.

    A CLEAN process-start reconciler lets sandbox start (it requires one, spec §6);
    the per-session broker then diverges — the realistic "state drifted mid-day"
    case."""
    cfg = _config(tmp_path, "sandbox")
    app = Application(cfg, reconciler=Reconciler(FakeBrokerAccount([]), InMemoryPositionStore()))
    await app.startup()  # process-start reconcile clean -> starts (migrates the DB)
    try:
        with pytest.raises(SessionAborted):
            await _reconcile_or_abort(
                FakeBrokerAccount([_UNEXPECTED]), cfg, app.audit,
                lambda *_: None, app._halt_process,
            )
        # Stop is set AND the halt is engaged — but the process has NOT torn down
        # yet, and the trip (with detail) is ALREADY durable on the writer's conn.
        assert app.killswitch.engaged is True
        assert app._stop.is_set() is True
        trip = app.audit._conn.execute(
            "SELECT payload FROM audit_log WHERE category='kill_switch.trip'"
        ).fetchone()
        assert trip is not None
        assert '"source": "reconciliation_mismatch"' in trip["payload"]
        assert "unexpected_at_broker" in trip["payload"]  # detail rode along in the trip
    finally:
        await app.shutdown()

    # Reopen the DB from an INDEPENDENT connection (the process is fully torn down):
    # both the attributed trip and the standalone verdict survived the halt, so the
    # phone view going dark costs no forensic detail.
    conn = connect(cfg.audit.db_path)
    try:
        trip = conn.execute(
            "SELECT payload FROM audit_log WHERE category='kill_switch.trip'"
        ).fetchone()
        verdicts = [
            r["payload"]
            for r in conn.execute("SELECT payload FROM audit_log WHERE category='reconcile.result'")
        ]
    finally:
        conn.close()
    assert trip is not None and "unexpected_at_broker" in trip["payload"]
    assert any("unexpected_at_broker" in v for v in verdicts)  # independent verdict record too


@pytest.mark.parametrize("environment", ["shadow", "sandbox", "live"])
async def test_clean_reconcile_returns_quietly_in_every_env(tmp_path, environment):
    """A clean reconcile never halts and never aborts — the gating only bites on a
    mismatch, in every environment."""
    cfg = _config(tmp_path, environment)
    _prepare_db(cfg.audit.db_path)
    audit, halt, alerts = _StubAudit(), _HaltSpy(), []

    await _reconcile_or_abort(
        FakeBrokerAccount([]), cfg, audit, lambda k, d: alerts.append(k), halt,
    )

    assert halt.calls == []
    assert alerts == []
    # Position-only when no execution port is wired (shadow, and any caller that
    # passes no stack), so `order_discrepancies` is structurally empty. The
    # `trading_date` stamp is what the NEXT run's sandbox-reset rollover check reads
    # back (`reconcile.last_clean_reconciliation`), so it is on every result.
    assert audit.records == [
        (
            "reconcile.result",
            {
                "ok": True,
                "discrepancies": [],
                "order_discrepancies": [],
                "trading_date": et_today().isoformat(),
            },
        )
    ]
