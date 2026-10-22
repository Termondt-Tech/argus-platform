"""The order ledger and the fill -> `positions` path.

Idempotency is the property under test throughout: `PlacedOrder` events carry
CUMULATIVE per-leg quantities and are re-sent on every status change, so "apply
the same event twice" and "apply an event that adds one more contract" have to
come out right or the expected-position set reconciliation reads is fiction.

Runs against the real `AuditLog.write` seam (the production gateway) so the
sharing of the process's single writer connection is exercised, not stubbed.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from argus.audit.log import AuditLog
from argus.execution.models import (
    LegAction,
    LegFill,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)
from argus.execution.store import InMemoryOrderStore, SqliteOrderStore, signed_leg_delta
from argus.reconcile import SqlitePositionStore

SHORT = "SPXW  260729P05000000"
LONG = "SPXW  260729P04995000"


@pytest.fixture
def store(audit: AuditLog) -> SqliteOrderStore:
    return SqliteOrderStore(audit.write, clock=lambda: "2026-07-29T14:00:00.000Z")


def spread(
    *,
    order_id: int | None = 1000,
    quantity: int = 1,
    remaining: tuple[int | None, int | None] = (None, None),
    status: OrderStatus | None = None,
    fills: tuple[tuple[LegFill, ...], tuple[LegFill, ...]] = ((), ()),
    identifier: str = "argus-open-20260729-put-5000-4995",
    direction: OrderDirection = OrderDirection.OPEN,
) -> Order:
    short_action, long_action = (
        (LegAction.SELL_TO_OPEN, LegAction.BUY_TO_OPEN)
        if direction is OrderDirection.OPEN
        else (LegAction.BUY_TO_CLOSE, LegAction.SELL_TO_CLOSE)
    )
    return Order(
        direction=direction,
        underlying="SPXW",
        legs=(
            OrderLeg(SHORT, short_action, quantity, remaining[0], fills=fills[0]),
            OrderLeg(LONG, long_action, quantity, remaining[1], fills=fills[1]),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
        candidate_id="20260729-put-5000-4995",
        broker_order_id=order_id,
        status=status,
    )


def positions(db: sqlite3.Connection) -> dict[str, int]:
    return SqlitePositionStore(db).open_positions()


# -- signed arithmetic --------------------------------------------------------


@pytest.mark.parametrize(
    "action,expected",
    [
        (LegAction.SELL_TO_OPEN, -2),
        (LegAction.SELL_TO_CLOSE, -2),
        (LegAction.BUY_TO_OPEN, 2),
        (LegAction.BUY_TO_CLOSE, 2),
    ],
)
def test_signed_leg_delta_is_buy_sell_not_open_close(action, expected):
    # BUY_TO_CLOSE on a short leg adds a POSITIVE quantity that walks the position
    # back toward flat — same arithmetic as BUY_TO_OPEN. Keeping it that way is
    # what lets a closing spread need no special case anywhere.
    assert signed_leg_delta(action, 2) == expected


# -- booking a fill -----------------------------------------------------------


async def test_full_fill_writes_the_expected_position_set(store, db):
    applied = await store.apply(
        spread(remaining=(0, 0), status=OrderStatus.FILLED)
    )

    assert {a.symbol: a.signed_delta for a in applied} == {SHORT: -1, LONG: 1}
    # This is the whole point of the chunk: reconciliation's expected set is no
    # longer structurally empty.
    assert positions(db) == {SHORT: -1, LONG: 1}


async def test_reapplying_the_same_event_books_nothing(store, db):
    order = spread(remaining=(0, 0), status=OrderStatus.FILLED)
    await store.apply(order)
    again = await store.apply(order)

    assert again == ()                      # no second AppliedFill, so no second audit line
    assert positions(db) == {SHORT: -1, LONG: 1}   # and no doubled position


async def test_partial_then_full_books_only_the_delta(store, db):
    # A 3-lot filling 1, then the remaining 2. The stream sends cumulative
    # remaining_quantity, so the second event must contribute 2, not 3.
    first = await store.apply(spread(quantity=3, remaining=(2, 2), status=OrderStatus.LIVE))
    assert {a.contracts for a in first} == {1}
    assert positions(db) == {SHORT: -1, LONG: 1}

    second = await store.apply(spread(quantity=3, remaining=(0, 0), status=OrderStatus.FILLED))
    assert {a.contracts for a in second} == {2}
    assert positions(db) == {SHORT: -3, LONG: 3}


async def test_an_out_of_order_event_cannot_unwind_a_position(store, db):
    await store.apply(spread(quantity=3, remaining=(0, 0), status=OrderStatus.FILLED))
    # A late-delivered earlier event reporting LESS filled than we have applied.
    stale = await store.apply(spread(quantity=3, remaining=(2, 2), status=OrderStatus.LIVE))

    assert stale == ()
    assert positions(db) == {SHORT: -3, LONG: 3}   # fills do not un-happen


async def test_unreported_quantities_book_nothing(store, db):
    # `remaining_quantity is None` is "no news", NOT "zero filled". Treating it as
    # zero would book the full order on the acknowledgement.
    assert await store.apply(spread(remaining=(None, None), status=OrderStatus.RECEIVED)) == ()
    assert positions(db) == {}


async def test_closing_the_spread_nets_to_flat_and_removes_the_rows(store, db):
    await store.apply(spread(remaining=(0, 0), status=OrderStatus.FILLED))
    await store.apply(
        spread(
            order_id=1001,
            remaining=(0, 0),
            status=OrderStatus.FILLED,
            direction=OrderDirection.CLOSE,
            identifier="argus-close-20260729-put-5000-4995",
        )
    )

    assert positions(db) == {}
    # Flat rows are DELETED, not left at zero — the table stays equal to what
    # `open_positions()` filters for instead of accumulating dead 0DTE strikes.
    assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


# -- cost basis (best-effort, never safety-critical) --------------------------


async def test_reported_fills_set_the_cost_basis(store, db):
    order = spread(
        remaining=(0, 0),
        status=OrderStatus.FILLED,
        fills=((LegFill(1, 1.20, "f1"),), (LegFill(1, 0.35, "f2"),)),
    )
    await store.apply(order)

    basis = dict(db.execute("SELECT symbol, average_open_price FROM positions").fetchall())
    assert basis[SHORT] == pytest.approx(1.20)
    assert basis[LONG] == pytest.approx(0.35)


async def test_adding_to_a_position_weights_the_basis(store, db):
    await store.apply(
        spread(quantity=2, remaining=(1, 1), status=OrderStatus.LIVE,
               fills=((LegFill(1, 1.00),), (LegFill(1, 0.20),)))
    )
    await store.apply(
        spread(quantity=2, remaining=(0, 0), status=OrderStatus.FILLED,
               fills=((LegFill(1, 1.00), LegFill(1, 2.00)),
                      (LegFill(1, 0.20), LegFill(1, 0.40))))
    )

    basis = dict(db.execute("SELECT symbol, average_open_price FROM positions").fetchall())
    assert basis[SHORT] == pytest.approx(1.50)   # (1.00 + 2.00) / 2
    assert basis[LONG] == pytest.approx(0.30)


async def test_a_fill_with_no_reported_price_lands_at_zero_not_a_guess(store, db):
    # 0.0 reads as "the broker reported no per-fill detail". Nothing
    # safety-critical consumes this column — reconciliation compares symbol and
    # signed quantity only.
    await store.apply(spread(remaining=(0, 0), status=OrderStatus.FILLED))
    basis = dict(db.execute("SELECT symbol, average_open_price FROM positions").fetchall())
    assert basis[SHORT] == 0.0


async def test_an_unpriced_addition_carries_the_existing_basis(store, db):
    await store.apply(
        spread(quantity=2, remaining=(1, 1), status=OrderStatus.LIVE,
               fills=((LegFill(1, 1.00),), (LegFill(1, 0.20),)))
    )
    # Second contract fills, no fill detail this time: better to carry a slightly
    # stale basis than to blend a zero in and halve it.
    await store.apply(spread(quantity=2, remaining=(0, 0), status=OrderStatus.FILLED))

    basis = dict(db.execute("SELECT symbol, average_open_price FROM positions").fetchall())
    assert basis[SHORT] == pytest.approx(1.00)


def test_fill_price_slices_by_cumulative_quantity():
    leg = OrderLeg(SHORT, LegAction.SELL_TO_OPEN, 3, 0,
                   fills=(LegFill(1, 1.00), LegFill(2, 2.00)))
    assert leg.fill_price(0, 1) == pytest.approx(1.00)
    assert leg.fill_price(1, 3) == pytest.approx(2.00)
    assert leg.fill_price(0, 3) == pytest.approx(5.0 / 3.0)
    assert leg.fill_price(3, 3) is None          # empty slice
    assert OrderLeg(SHORT, LegAction.SELL_TO_OPEN, 1, 0).fill_price(0, 1) is None


# -- the ledger itself --------------------------------------------------------


async def test_record_order_makes_an_order_accounted_for(store):
    await store.record_order(spread(order_id=1234))
    assert await store.known_orders([1234, 9999]) == {1234}


async def test_a_dry_run_records_nothing(store):
    # A dry run mints no broker id (measured against sandbox), so there is nothing
    # at the broker to account for. Recording it would invent an order.
    await store.record_order(spread(order_id=None))
    assert await store.known_orders([]) == set()


async def test_known_orders_is_empty_for_a_fresh_argus(store):
    assert await store.known_orders([1, 2, 3]) == set()


async def test_applying_a_fill_also_makes_the_order_known(store):
    # A fill for an order recorded by a previous process still has to become
    # accounted-for, or the next restart halts over an order it just booked.
    await store.apply(spread(order_id=777, remaining=(0, 0), status=OrderStatus.FILLED))
    assert await store.known_orders([777]) == {777}


async def test_candidate_id_survives_an_event_that_lost_it(store, db):
    # candidate_id never round-trips through the broker, so a streamed event has
    # None there. It must not erase what the submission recorded.
    await store.record_order(spread(order_id=42))
    streamed = spread(order_id=42, remaining=(0, 0), status=OrderStatus.FILLED)
    await store.apply(
        Order(**{**streamed.__dict__, "candidate_id": None})
    )
    row = db.execute(
        "SELECT candidate_id, status FROM orders WHERE broker_order_id = 42"
    ).fetchone()
    assert row[0] == "20260729-put-5000-4995"
    assert row[1] == "Filled"


async def test_direction_is_first_write_wins(store, db):
    """`orders.direction` is INSERT-only (pinned 2026-09-03): the value the
    submission recorded is the value the row keeps for life, and neither a later
    event nor any backfill repairs or corrupts it. The second write here carries
    the direction the PRE-fix mapper would have inferred from relabelled legs —
    exactly what an `ON CONFLICT DO UPDATE` on this column would let through."""
    await store.record_order(
        spread(
            order_id=50,
            direction=OrderDirection.CLOSE,
            identifier="argus-close-20260729-put-5000-4995",
        )
    )
    # The same broker order, mapped the way the old code mapped it: relabelled
    # legs, inferred OPEN.
    await store.record_order(
        spread(
            order_id=50,
            direction=OrderDirection.OPEN,
            identifier="argus-close-20260729-put-5000-4995",
        )
    )
    row = db.execute(
        "SELECT direction FROM orders WHERE broker_order_id = 50"
    ).fetchone()
    assert row[0] == "close"


# -- the in-memory twin behaves identically -----------------------------------


async def test_in_memory_store_matches_the_sqlite_semantics():
    store = InMemoryOrderStore()
    order = spread(remaining=(0, 0), status=OrderStatus.FILLED)

    assert len(await store.apply(order)) == 2
    assert await store.apply(order) == ()          # idempotent, same as SQLite
    assert store.positions == {SHORT: -1, LONG: 1}
    assert await store.known_orders([1000]) == {1000}


# -- the single-writer boundary, asserted structurally ------------------------


def test_store_py_is_the_only_module_that_writes_positions():
    """`positions` has exactly ONE writer, and it is this module.

    Structural because the alternative is invisible: a second writer elsewhere in
    the tree would work perfectly in isolation and only surface as an expected-state
    row nothing accounts for — a reconciliation mismatch, i.e. a hard halt, at some
    later restart. Two features have already wanted to reach for the table directly
    (the sandbox 24h wipe, expired-position resolution); both went through the store
    instead, and this is what keeps the third one honest.

    Scanned as AST string constants rather than by grep so a comment mentioning the
    SQL does not trip it, and so the check follows the code however it is formatted.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "argus"
    writes = re.compile(
        r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+positions\b", re.IGNORECASE
    )
    found = [
        f"{path.relative_to(src)}:{node.lineno}"
        for path in sorted(src.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and writes.search(node.value)
    ]

    assert found, "the scan found no writes at all — it has stopped checking anything"
    offenders = [f for f in found if not f.startswith("execution/store.py")]
    assert offenders == [], f"only execution/store.py may write `positions`; found {offenders}"
