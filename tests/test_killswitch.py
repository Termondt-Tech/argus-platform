"""Kill switch: immediate halt, Tier 1 trip record, idempotent."""

from __future__ import annotations

import pytest

from argus.audit.log import AuditLog
from argus.risk import HaltedError, KillSwitch


async def test_engage_halts_and_records_trip(audit: AuditLog):
    ks = KillSwitch(audit)
    assert ks.engaged is False
    ks.check()  # does not raise while disengaged

    await ks.engage("manual halt", source="phone")
    assert ks.engaged is True
    assert ks.reason == "manual halt"

    with pytest.raises(HaltedError, match="manual halt"):
        ks.check()

    # A Tier 1 trip record must be in the audit log.
    row = audit._conn.execute(
        "SELECT category, payload FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row["category"] == "kill_switch.trip"
    assert "phone" in row["payload"]
    audit.verify_chain()


async def test_engage_is_idempotent(audit: AuditLog):
    ks = KillSwitch(audit)
    await ks.engage("first", source="watchdog")
    await ks.engage("second", source="phone")
    assert ks.reason == "first"  # original reason preserved
    # Only one trip record written.
    count = audit._conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE category = 'kill_switch.trip'"
    ).fetchone()[0]
    assert count == 1


async def test_engage_without_detail_keeps_minimal_payload(audit: AuditLog):
    # Callers that don't pass detail get exactly {reason, source} — no stray key,
    # so the many existing engage sites are unchanged.
    import json

    ks = KillSwitch(audit)
    await ks.engage("manual halt", source="phone")
    row = audit._conn.execute(
        "SELECT payload FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert set(json.loads(row["payload"])) == {"reason", "source"}


async def test_engage_records_optional_detail_for_later_review(audit: AuditLog):
    # A reconcile-mismatch trip carries the discrepancy detail nested under
    # "detail" so the trip record alone tells the whole story (spec §6 / /status).
    import json

    ks = KillSwitch(audit)
    await ks.engage(
        "broker reconciliation mismatch (1 discrepancy(ies))",
        source="reconciliation_mismatch",
        detail={"ok": False, "discrepancies": [
            {"symbol": "SPXW260718P05000000", "kind": "unexpected_at_broker",
             "expected_qty": 0, "broker_qty": -1},
        ]},
    )
    row = audit._conn.execute(
        "SELECT payload FROM audit_log ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row["payload"])
    assert payload["source"] == "reconciliation_mismatch"
    assert payload["detail"]["discrepancies"][0]["kind"] == "unexpected_at_broker"
    audit.verify_chain()
