"""Fill consumption off the `AlertStreamer` order feed.

`AlertStreamerAccount` already decoded and dispatched `PlacedOrder` frames before
this chunk — `_listen_orders` has run a `streamer.listen(PlacedOrder)` iterator
into an injected `on_order` callback since the account layer was built. There was
simply no consumer. These tests cover the consumer, plus the wiring assertion that
it drops into that existing hook unchanged.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from argus.account.stream import AlertStreamerAccount
from argus.audit.log import AuditLog
from argus.execution.fills import FillConsumer
from argus.execution.store import InMemoryOrderStore, SqliteOrderStore
from argus.reconcile import SqlitePositionStore

SHORT = "SPXW  260729P05000000"
LONG = "SPXW  260729P04995000"
OURS = "argus-open-20260729-put-5000-4995"


def placed(
    *,
    order_id: int = 1000,
    remaining: tuple[int | None, int | None] = (0, 0),
    status: str = "Filled",
    identifier: str = OURS,
    legs: tuple | None = None,
    reject_reason: str | None = None,
):
    """A duck-typed stand-in for the SDK's `PlacedOrder`, as the stream delivers it."""
    default_legs = (
        SimpleNamespace(
            symbol=SHORT, action="Sell to Open", quantity=1,
            remaining_quantity=remaining[0], instrument_type="Equity Option", fills=None,
        ),
        SimpleNamespace(
            symbol=LONG, action="Buy to Open", quantity=1,
            remaining_quantity=remaining[1], instrument_type="Equity Option", fills=None,
        ),
    )
    return SimpleNamespace(
        id=order_id,
        legs=default_legs if legs is None else legs,
        price="0.85",
        underlying_symbol="SPXW",
        external_identifier=identifier,
        status=status,
        replaces_order_id=None,
        replacing_order_id=None,
        reject_reason=reject_reason,
    )


class _Audit:
    """Captures both tiers so the tiering itself can be asserted."""

    def __init__(self) -> None:
        self.tier1: list[tuple[str, dict]] = []
        self.tier2: list[tuple[str, dict]] = []

    async def record(self, category, payload):
        self.tier1.append((category, payload))

    def telemetry(self, category, payload):
        self.tier2.append((category, payload))
        return True


@pytest.fixture
def wiring():
    store, audit, alerts = InMemoryOrderStore(), _Audit(), []
    consumer = FillConsumer(
        store=store, audit=audit, alert=lambda k, d: alerts.append((k, d))
    )
    return consumer, store, audit, alerts


# -- the transport it plugs into ---------------------------------------------


def test_it_fits_the_stream_hook_that_already_existed():
    # The consumer is callable with one order argument, which is exactly the
    # `on_order` contract `AlertStreamerAccount._listen_orders` already invokes.
    # No change to the streamer was needed to consume fills.
    consumer = FillConsumer(store=InMemoryOrderStore(), audit=_Audit())
    stream = AlertStreamerAccount(
        object(), SimpleNamespace(account_number="X"), clock=lambda: 0.0,
        on_order=consumer,
    )
    assert stream._on_order is consumer


# -- booking --------------------------------------------------------------


async def test_a_fill_updates_positions_and_writes_tier_1(wiring):
    consumer, store, audit, _ = wiring
    await consumer(placed())

    assert store.positions == {SHORT: -1, LONG: 1}
    assert [c for c, _ in audit.tier1] == ["order.fill", "order.fill"]
    assert audit.tier1[0][1]["contracts"] == 1
    assert audit.tier1[0][1]["candidate_id"] is None   # not recoverable from the broker


async def test_a_status_change_with_no_new_fill_is_telemetry(wiring):
    consumer, store, audit, _ = wiring
    await consumer(placed(remaining=(1, 1), status="Live"))

    # By volume this is the common case — the stream re-sends the order on every
    # status change. Tier 1 would drown in it.
    assert store.positions == {}
    assert audit.tier1 == []
    assert [c for c, _ in audit.tier2] == ["order.event"]
    assert audit.tier2[0][1]["disposition"] == "working"


async def test_a_repeated_fill_event_writes_no_second_record(wiring):
    consumer, _, audit, _ = wiring
    await consumer(placed())
    await consumer(placed())

    # One movement, one pair of Tier 1 records; the repeat degrades to telemetry.
    assert [c for c, _ in audit.tier1] == ["order.fill", "order.fill"]
    assert [c for c, _ in audit.tier2] == ["order.event"]


# -- a rejection is the END of a trade idea, not a status flap ------------
#
# 2026-08-27: two orders were routed and rejected inside a second (1422817,
# 1422841) and Argus recorded no reason for either. The field was on the SDK's
# `PlacedOrder` the whole time; the mapping never read it, and the record that
# would have carried it was Tier 2 — queued and droppable.


async def test_a_rejection_records_the_brokers_reason(wiring):
    consumer, _, audit, _ = wiring
    await consumer(
        placed(remaining=(1, 1), status="Rejected", reject_reason="Insufficient buying power")
    )

    (category, payload), = audit.tier1
    assert category == "order.event"
    assert payload["reject_reason"] == "Insufficient buying power"
    assert payload["disposition"] == "dead"


async def test_a_terminal_event_is_tier_1_not_telemetry(wiring):
    """Tier 2 is right for a flap and wrong for the end of a trade idea.

    Capturing the reason is pointless if the only record carrying it can be
    dropped under queue pressure.
    """
    consumer, _, audit, _ = wiring
    await consumer(placed(remaining=(1, 1), status="Rejected", reject_reason="nope"))

    assert [c for c, _ in audit.tier1] == ["order.event"]
    assert audit.tier2 == []


async def test_a_repeated_rejection_is_recorded_once(wiring):
    """The AlertStreamer re-delivers current state on reconnect, and a Tier 1 write
    is awaited, holds the writer lock and fsyncs. 2026-08-13 put 1,517 of them in
    the log from one flapping condition; the promotion must not reopen that."""
    consumer, _, audit, _ = wiring
    for _ in range(3):
        await consumer(placed(remaining=(1, 1), status="Rejected", reject_reason="nope"))

    assert [c for c, _ in audit.tier1] == ["order.event"]
    assert [c for c, _ in audit.tier2] == ["order.event", "order.event"]
    # The repeats still carry the reason — only their TIER is downgraded.
    assert all(p["reject_reason"] == "nope" for _, p in audit.tier2)


async def test_two_rungs_of_one_walk_each_record_their_own_rejection(wiring):
    """Deduped on broker order id, never on `external_identifier`.

    A walk reprices, so several orders share one identifier — the same "one
    identifier, many orders" trap the market-crossing confirmation is scoped
    around. Keying on the identifier would swallow the second rung's rejection.
    """
    consumer, _, audit, _ = wiring
    await consumer(placed(order_id=1, remaining=(1, 1), status="Rejected", reject_reason="a"))
    await consumer(placed(order_id=2, remaining=(1, 1), status="Rejected", reject_reason="b"))

    assert [p["reject_reason"] for _, p in audit.tier1] == ["a", "b"]


async def test_a_working_order_stays_telemetry_and_carries_a_null_reason(wiring):
    # The promotion is scoped to DEAD. A live order re-sent on every status change
    # is still the common case by volume and must not reach Tier 1.
    consumer, _, audit, _ = wiring
    await consumer(placed(remaining=(1, 1), status="Live"))

    assert audit.tier1 == []
    assert audit.tier2[0][1]["reject_reason"] is None


async def test_a_filled_order_is_never_treated_as_terminal_here(wiring):
    """FILLED is terminal in the lifecycle sense and is the one thing that must not
    be filed next to a cancellation — its record is `order.fill`, on the applied
    path. A repeat degrades to telemetry exactly as before."""
    consumer, _, audit, _ = wiring
    await consumer(placed())
    await consumer(placed())

    assert [c for c, _ in audit.tier1] == ["order.fill", "order.fill"]
    assert [c for c, _ in audit.tier2] == ["order.event"]


async def test_partial_fills_are_recorded_as_quantities(wiring):
    consumer, store, audit, _ = wiring
    await consumer(placed(remaining=(2, 2), status="Live"))   # 3-lot, 1 filled

    legs = (
        SimpleNamespace(symbol=SHORT, action="Sell to Open", quantity=3,
                        remaining_quantity=2, instrument_type="Equity Option", fills=None),
        SimpleNamespace(symbol=LONG, action="Buy to Open", quantity=3,
                        remaining_quantity=2, instrument_type="Equity Option", fills=None),
    )
    await consumer(placed(legs=legs, status="Live"))

    assert store.positions == {SHORT: -1, LONG: 1}
    assert audit.tier1[0][1]["contracts"] == 1


# -- what it refuses to adopt -------------------------------------------------


async def test_a_foreign_order_is_recorded_and_alerted_but_never_booked(wiring):
    consumer, store, audit, alerts = wiring
    await consumer(placed(identifier="someone-elses-order"))

    # Argus's position record is a record of ARGUS's positions. Absorbing a
    # hand-placed fill would make reconciliation agree with the broker by
    # rewriting what it expected — the exact guess spec §6 forbids. Leaving it
    # out is what lets the position rail catch it.
    assert store.positions == {}
    assert [c for c, _ in audit.tier1] == ["order.foreign"]
    assert [k for k, _ in alerts] == ["foreign_order_event"]


async def test_an_unmappable_event_is_loud_and_does_not_escape(wiring):
    consumer, store, audit, alerts = wiring
    broken = placed()
    broken.legs[0].action = "Teleport to Open"

    await consumer(broken)   # must NOT raise: an exception here tears down the
                             # account stream's _pump and takes positions and
                             # balances down with it.
    assert store.positions == {}
    assert [c for c, _ in audit.tier1] == ["order.unmappable"]
    assert [k for k, _ in alerts] == ["order_event_unmappable"]


# -- end to end against the real database ------------------------------------


async def test_fills_land_in_the_positions_table_reconciliation_reads(
    audit: AuditLog, db: sqlite3.Connection
):
    store = SqliteOrderStore(audit.write)
    await FillConsumer(store=store, audit=audit)(placed())

    # The chunk's headline: the expected-position set is no longer permanently
    # empty, and it is the same table `SqlitePositionStore` already read from.
    assert SqlitePositionStore(db).open_positions() == {SHORT: -1, LONG: 1}


def test_the_foreign_fill_alert_is_never_silently_omitted(audit):
    """Chunk 2 documented this path as "Tier 1 + alert", but `alert` defaulted to None
    — so a caller that omitted it turned that into "Tier 1 only", silently. Chunk 4
    built the real sink (`argus.notify`), and the default is now `log_alert`:
    forgetting to pass one degrades to a log line rather than to nothing."""
    from argus.alerts import log_alert
    from argus.execution.fills import FillConsumer
    from argus.execution.store import InMemoryOrderStore

    consumer = FillConsumer(store=InMemoryOrderStore(), audit=audit)
    assert consumer._alert is log_alert
