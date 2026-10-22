"""Audit record model and canonical hashing.

The hash chain is what makes this a tamper-evident black box rather than
best-effort logging. `canonical_bytes` defines the exact byte sequence that gets
hashed; it must be stable across processes and Python versions, so it uses
sorted keys and compact separators and forbids non-JSON payloads.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

# Genesis link for the first record in an empty chain.
GENESIS_HASH = "0" * 64


class Tier(IntEnum):
    """Audit write tiers (spec §6).

    SEQUENCED (1): the caller awaits the write before proceeding — pre-trade gate
    decisions immediately preceding an order, order submission/fills, kill-switch
    trips, daily-loss-breach events. These carry the crash-survival guarantee.

    TELEMETRY (2): no-action signal evaluations and routine re-checks. Enqueued
    to a bounded background writer; losing a handful in an ungraceful crash is
    not a safety gap. Never used for Tier 1 events.
    """

    SEQUENCED = 1
    TELEMETRY = 2


def canonical_bytes(
    seq: int, ts: str, tier: int, category: str, payload: dict[str, Any], prev_hash: str
) -> bytes:
    """Deterministic byte encoding of a record for hashing."""
    return json.dumps(
        {
            "seq": seq,
            "ts": ts,
            "tier": tier,
            "category": category,
            "payload": payload,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def compute_hash(
    seq: int, ts: str, tier: int, category: str, payload: dict[str, Any], prev_hash: str
) -> str:
    return hashlib.sha256(
        canonical_bytes(seq, ts, tier, category, payload, prev_hash)
    ).hexdigest()


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: str
    tier: int
    category: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str

    @staticmethod
    def build(
        *,
        seq: int,
        ts: str,
        tier: Tier,
        category: str,
        payload: dict[str, Any],
        prev_hash: str,
    ) -> "AuditRecord":
        h = compute_hash(seq, ts, int(tier), category, payload, prev_hash)
        return AuditRecord(
            seq=seq,
            ts=ts,
            tier=int(tier),
            category=category,
            payload=payload,
            prev_hash=prev_hash,
            hash=h,
        )

    def recompute_hash(self) -> str:
        return compute_hash(
            self.seq, self.ts, self.tier, self.category, self.payload, self.prev_hash
        )
