"""Reassembling held spreads from the order ledger (`store.open_spreads`).

This is where the watchdog's entry-credit basis comes from, and it exists because
`positions` cannot supply it: that table is keyed by OCC LEG symbol, has no
spread-level row, and its only price column is documented as best-effort and
safety-irrelevant. So the pairing logic below is what stands between a 2x-credit
stop and a stop measured against a made-up number.

The SQLite store and the in-memory one run the SAME pairing function, and one test
here asserts they agree on a non-trivial case — a fake that paired differently from
production would make every other test in the watchdog suite meaningless.

Every read is `as of` a trading date (`LEDGER_DAY`), because a leg whose own OCC
expiry is strictly in the past is no longer outstanding — the third way a leg
stops being open, alongside a closing fill and never having filled. `LEDGER_DAY`
is the fixtures' own expiry, so the ordinary cases below read as legs expiring
TODAY: live, watchable, and untouched by that rule. The expiry cases at the
bottom of this file are the ones that move the date past it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from argus.execution.models import (
    LegAction,
    LegFill,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)
from argus.execution.store import CreditBasis, InMemoryOrderStore, SqliteOrderStore

PUT_SHORT = "SPXW  260729P05000000"
PUT_LONG = "SPXW  260729P04995000"

#: The trading date every read below is taken as of: the fixtures' own 0DTE
#: expiry. Anything dated later in this file (the call spreads at 260817) is a
#: FUTURE expiry from here, which is equally not-expired — both are live legs.
LEDGER_DAY = date(2026, 7, 29)


def opening(
    *,
    order_id: int = 1000,
    credit: str = "0.85",
    contracts: int = 2,
    filled: int | None = None,
    short: str = PUT_SHORT,
    long_: str = PUT_LONG,
    candidate: str | None = "20260729-put-5000-4995",
    direction: OrderDirection = OrderDirection.OPEN,
    short_price: float | None = None,
    long_price: float | None = None,
    priced_contracts: int | None = None,
) -> Order:
    """An opening credit spread as the broker would report it after a fill.

    `short_price`/`long_price` attach the per-execution detail the broker sends
    when it sends any (`LegFill`). Omitted, the order fills with no price detail —
    which is a real state, is what every fixture in this file did before
    2026-09-02, and is what makes the credit fall back to the LIMIT.

    `priced_contracts` covers the third state: the broker priced only PART of what
    it booked. `fill_price` returns the average over the part it covered and the
    store carries that against the whole applied slice.
    """
    remaining = 0 if filled is None else contracts - filled
    booked = contracts if filled is None else filled
    sell, buy = (
        (LegAction.SELL_TO_OPEN, LegAction.BUY_TO_OPEN)
        if direction is OrderDirection.OPEN
        else (LegAction.BUY_TO_CLOSE, LegAction.SELL_TO_CLOSE)
    )
    sign = 1 if direction is OrderDirection.OPEN else -1

    def fills(price: float | None) -> tuple[LegFill, ...]:
        if price is None:
            return ()
        quantity = booked if priced_contracts is None else priced_contracts
        return () if quantity <= 0 else (LegFill(quantity=quantity, price=price),)

    return Order(
        direction=direction,
        underlying="SPXW",
        legs=(
            OrderLeg(
                short, sell, contracts,
                remaining_quantity=remaining, fills=fills(short_price),
            ),
            OrderLeg(
                long_, buy, contracts,
                remaining_quantity=remaining, fills=fills(long_price),
            ),
        ),
        limit_price=Decimal(credit) * sign,
        external_identifier=f"argus-{direction}-{candidate}",
        candidate_id=candidate,
        broker_order_id=order_id,
        status=OrderStatus.FILLED if remaining == 0 else OrderStatus.LIVE,
    )


async def sqlite_store(db: sqlite3.Connection) -> SqliteOrderStore:
    async def run(fn):
        return fn(db)

    return SqliteOrderStore(run)


# -- the happy path ------------------------------------------------------------


async def test_entry_credit_falls_back_to_the_limit_when_the_broker_priced_nothing():
    """The whole point of this module. There is no credit field in `positions`.

    No `LegFill` detail, so there is no realized price to prefer — the pre-0005
    behaviour for every position, and still the right answer whenever the broker
    books contracts without pricing them.
    """
    store = InMemoryOrderStore()
    await store.apply(opening(credit="0.85"))

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert len(scan.spreads) == 1
    spread = scan.spreads[0]
    assert spread.entry_credit == Decimal("0.85")
    assert spread.entry_credit_basis is CreditBasis.LIMIT
    assert spread.entry_limit == Decimal("0.85")
    assert spread.fills_credit is None
    assert spread.priced_contracts == 0
    assert (spread.short_symbol, spread.long_symbol) == (PUT_SHORT, PUT_LONG)
    assert spread.contracts == 2
    assert not spread.imbalanced
    assert scan.fully_explained


async def test_contracts_come_from_what_was_APPLIED_not_from_what_was_ordered():
    """The order says what was REQUESTED; `applied_quantity` says what actually filled."""
    store = InMemoryOrderStore()
    await store.apply(opening(contracts=3, filled=1))

    spread = (await store.open_spreads(today=LEDGER_DAY)).spreads[0]
    assert spread.contracts == 1


async def test_a_closed_spread_is_not_watched():
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000))
    # The close nets the positions to flat, and flat rows are deleted.
    await store.apply(opening(order_id=1001, direction=OrderDirection.CLOSE))

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert scan.fully_explained


async def test_a_closing_order_never_becomes_a_watched_spread():
    """Only `direction = open` can have left a position to watch."""
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1001, direction=OrderDirection.CLOSE))
    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    # The close DID move positions (to short-the-long / long-the-short), and no
    # opening order accounts for that, so it must be reported rather than ignored.
    assert scan.unattributed == tuple(sorted((PUT_LONG, PUT_SHORT)))


async def test_an_unfilled_opening_order_is_not_a_position():
    store = InMemoryOrderStore()
    await store.record_order(opening(filled=0))
    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert scan.fully_explained


# -- attribution ---------------------------------------------------------------


async def test_the_most_recent_opening_order_owns_the_position():
    """The same candidate can be re-entered after a close — the duplicate guard only
    blocks while a position is HELD — leaving two opening orders and a close over the
    same two symbols under ONE trade idea. They sum inside that group, so what is
    left is the re-entry, priced from the newer opening order."""
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000, credit="0.85"))
    await store.apply(opening(order_id=1001, direction=OrderDirection.CLOSE))
    await store.apply(opening(order_id=1002, credit="1.20"))

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert len(scan.spreads) == 1
    assert scan.spreads[0].entry_credit == Decimal("1.20")
    assert scan.spreads[0].broker_order_id == 1002
    assert scan.fully_explained


async def test_two_different_spreads_are_watched_separately():
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000, credit="0.85"))
    await store.apply(
        opening(
            order_id=1001,
            credit="0.70",
            short="SPXW  260729C05100000",
            long_="SPXW  260729C05105000",
            candidate="20260729-call-5100-5105",
        )
    )
    scan = await store.open_spreads(today=LEDGER_DAY)
    assert len(scan.spreads) == 2
    assert {float(s.entry_credit) for s in scan.spreads} == {0.85, 0.70}
    assert scan.fully_explained


# -- everything it refuses to guess at ----------------------------------------


async def test_a_position_no_opening_order_explains_is_reported():
    """Argus's own two records disagreeing. Distinct from a reconciliation mismatch
    (which compares against the BROKER) and just as much a gap: an open position
    nobody is watching."""
    store = InMemoryOrderStore()
    store.positions["SPXW  260729P04000000"] = -1

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert scan.unattributed == ("SPXW  260729P04000000",)
    assert not scan.fully_explained


async def test_a_one_legged_order_is_malformed_not_a_spread():
    store = InMemoryOrderStore()
    order = opening()
    single = Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(order.legs[0],),
        limit_price=Decimal("0.85"),
        external_identifier=order.external_identifier,
        candidate_id=order.candidate_id,
        broker_order_id=1000,
        status=OrderStatus.FILLED,
    )
    await store.apply(single)

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert len(scan.malformed) == 1
    assert "not one short and one long" in scan.malformed[0]["problem"]
    # And it says WHAT is outstanding, which is the actionable half.
    assert scan.malformed[0]["outstanding"] == {PUT_SHORT: -2}


async def test_an_opening_order_at_a_debit_is_refused_as_a_credit_basis():
    """`build_order` signs OPEN positive, so a negative limit on an opening order is
    a record that cannot be trusted to price a stop — and a stop priced off a
    negative credit would trigger immediately or never."""
    store = InMemoryOrderStore()
    bad = opening()
    await store.apply(
        Order(
            direction=OrderDirection.OPEN,
            underlying="SPXW",
            legs=bad.legs,
            limit_price=Decimal("-0.85"),
            external_identifier=bad.external_identifier,
            candidate_id=bad.candidate_id,
            broker_order_id=1000,
            status=OrderStatus.FILLED,
        )
    )
    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert "is not a credit" in scan.malformed[0]["problem"]


async def test_holdings_that_contradict_the_positions_table_are_malformed():
    """The invariant that keeps per-idea attribution honest.

    Attribution decides WHICH idea holds what; it must never invent a holding. So
    the sum across every spread has to equal `positions` symbol by symbol, and where
    it does not the spread is reported rather than watched — pairing it anyway would
    price a stop on a fiction.
    """
    store = InMemoryOrderStore()
    await store.apply(opening())
    # Flip the short leg long. The order ledger still says -2.
    store.positions[PUT_SHORT] = 2

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert "disagree with the positions table" in scan.malformed[0]["problem"]
    assert scan.malformed[0]["divergent"] == {
        PUT_SHORT: {"attributed": -2, "positions": 2}
    }


async def test_a_ledger_describing_positions_that_are_gone_is_not_watched():
    """The hazard per-idea attribution could have introduced, ruled out.

    Deriving holdings from `applied_quantity` instead of `positions` means an order
    ledger that outlives its positions — a sandbox reset clears `positions` — would
    otherwise rebuild yesterday's spreads as phantoms and hand them to the watchdog.
    """
    store = InMemoryOrderStore()
    await store.apply(opening())
    store.positions.clear()

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert "disagree with the positions table" in scan.malformed[0]["problem"]


async def test_an_imbalanced_spread_is_watched_but_flagged():
    """A partial fill can leave the legs unpaired, i.e. part of the position naked.
    It is still watchable at the paired size, but it must say so.

    Built from UNEQUAL APPLIED QUANTITIES, which is how an imbalance actually
    reaches the ledger now that holdings come from the order record rather than
    from `positions`.
    """
    store = InMemoryOrderStore()
    order = opening(contracts=2)
    partial = Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            order.legs[0],                                            # short: 2 filled
            OrderLeg(PUT_LONG, LegAction.BUY_TO_OPEN, 2, remaining_quantity=1),
        ),
        limit_price=order.limit_price,
        external_identifier=order.external_identifier,
        candidate_id=order.candidate_id,
        broker_order_id=order.broker_order_id,
        status=OrderStatus.LIVE,
    )
    await store.apply(partial)

    spread = (await store.open_spreads(today=LEDGER_DAY)).spreads[0]
    assert (spread.short_quantity, spread.long_quantity) == (2, 1)
    assert spread.contracts == 1
    assert spread.imbalanced


# -- the fake must not diverge from production --------------------------------


async def test_sqlite_and_in_memory_stores_pair_identically(db: sqlite3.Connection):
    """One pairing implementation, two stores. If these ever disagree, every
    watchdog test that uses the in-memory store is testing the wrong thing."""
    sqlite = await sqlite_store(db)
    memory = InMemoryOrderStore()

    for store in (sqlite, memory):
        await store.apply(opening(order_id=1000, credit="0.85"))
        await store.apply(opening(order_id=1001, direction=OrderDirection.CLOSE))
        await store.apply(opening(order_id=1002, credit="1.20"))
        await store.apply(
            opening(
                order_id=1003,
                credit="0.70",
                contracts=3,
                filled=2,
                short="SPXW  260729C05100000",
                long_="SPXW  260729C05105000",
                candidate="20260729-call-5100-5105",
            )
        )

    from_sqlite = await sqlite.open_spreads(today=LEDGER_DAY)
    from_memory = await memory.open_spreads(today=LEDGER_DAY)
    assert from_sqlite == from_memory
    assert len(from_sqlite.spreads) == 2
    assert from_sqlite.fully_explained


async def test_sqlite_store_reads_a_real_position_row(db: sqlite3.Connection):
    store = await sqlite_store(db)
    await store.apply(opening(credit="1.05", contracts=2))

    held = dict(db.execute("SELECT symbol, signed_quantity FROM positions").fetchall())
    assert held == {PUT_SHORT: -2, PUT_LONG: 2}
    spread = (await store.open_spreads(today=LEDGER_DAY)).spreads[0]
    assert spread.entry_credit == Decimal("1.05")
    assert spread.underlying == "SPXW"


async def test_open_spreads_on_an_empty_database_is_clean(db: sqlite3.Connection):
    scan = await (await sqlite_store(db)).open_spreads(today=LEDGER_DAY)
    assert scan == type(scan)()
    assert scan.fully_explained


@pytest.mark.parametrize("credit", ["0.05", "5.00"])
async def test_entry_credit_round_trips_exactly_as_a_decimal(credit: str):
    """A stop level is `multiple * credit`; a float-rounded basis would move it."""
    store = InMemoryOrderStore()
    await store.apply(opening(credit=credit))
    assert (await store.open_spreads(today=LEDGER_DAY)).spreads[0].entry_credit == Decimal(credit)


# -- overlapping verticals (the 2026-08-17 incident) --------------------------
#
# `signal.width = 5.0` against SPX's 5-point near-the-money ladder makes
# consecutive candidates CHAIN: 7755/7750 and 7750/7745 share the 7750 strike as
# one spread's short and the other's long. `positions` is keyed by symbol and holds
# only the NET, so 7750 nets to zero and its row is deleted — and the old
# symbol-keyed pairing lost BOTH spreads to that. Two live positions, no stop being
# evaluated on either. This is the common case for two same-side spreads, not an
# edge case.

PUT_7755 = "SPXW  260817P07755000"
PUT_7750 = "SPXW  260817P07750000"
PUT_7745 = "SPXW  260817P07745000"


def _vertical(*, order_id: int, short: str, long_: str, candidate: str, **kwargs) -> Order:
    return opening(
        order_id=order_id, short=short, long_=long_, candidate=candidate, **kwargs
    )


async def test_two_spreads_sharing_a_strike_are_both_watched():
    store = InMemoryOrderStore()
    await store.apply(
        _vertical(order_id=1344929, short=PUT_7750, long_=PUT_7745,
                  candidate="20260817-put-7750-7745")
    )
    await store.apply(
        _vertical(order_id=1345003, short=PUT_7755, long_=PUT_7750,
                  candidate="20260817-put-7755-7750")
    )
    # The shared strike really is flat in `positions` — sold 2, bought 2.
    assert PUT_7750 not in {s: q for s, q in store.positions.items() if q != 0}

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.malformed == (), "neither spread may be lost to the shared strike"
    assert scan.unattributed == ()
    assert scan.fully_explained

    by_candidate = {s.candidate_id: s for s in scan.spreads}
    assert set(by_candidate) == {
        "20260817-put-7750-7745",
        "20260817-put-7755-7750",
    }
    a = by_candidate["20260817-put-7750-7745"]
    assert (a.short_symbol, a.long_symbol) == (PUT_7750, PUT_7745)
    assert a.contracts == 2 and not a.imbalanced
    b = by_candidate["20260817-put-7755-7750"]
    assert (b.short_symbol, b.long_symbol) == (PUT_7755, PUT_7750)
    assert b.contracts == 2 and not b.imbalanced


async def test_closing_one_of_two_overlapping_spreads_leaves_the_other_watched():
    """The close must be attributed to ITS OWN idea, not netted across both."""
    store = InMemoryOrderStore()
    await store.apply(
        _vertical(order_id=1, short=PUT_7750, long_=PUT_7745,
                  candidate="20260817-put-7750-7745")
    )
    await store.apply(
        _vertical(order_id=2, short=PUT_7755, long_=PUT_7750,
                  candidate="20260817-put-7755-7750")
    )
    await store.apply(
        opening(order_id=3, short=PUT_7750, long_=PUT_7745,
                candidate="20260817-put-7750-7745", direction=OrderDirection.CLOSE)
    )

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.malformed == () and scan.unattributed == ()
    assert [s.candidate_id for s in scan.spreads] == ["20260817-put-7755-7750"]
    survivor = scan.spreads[0]
    assert (survivor.short_symbol, survivor.long_symbol) == (PUT_7755, PUT_7750)
    assert survivor.contracts == 2, "the surviving spread still holds its own 7750"


async def test_overlapping_spreads_pair_identically_in_both_stores(db: sqlite3.Connection):
    """The fake must not diverge from production on the case that broke live."""
    sqlite = await sqlite_store(db)
    memory = InMemoryOrderStore()
    for store in (sqlite, memory):
        await store.apply(
            _vertical(order_id=1344929, short=PUT_7750, long_=PUT_7745,
                      candidate="20260817-put-7750-7745")
        )
        await store.apply(
            _vertical(order_id=1345003, short=PUT_7755, long_=PUT_7750,
                      candidate="20260817-put-7755-7750")
        )
    assert await sqlite.open_spreads(today=LEDGER_DAY) == await memory.open_spreads(today=LEDGER_DAY)


async def test_a_stale_position_for_a_CLOSED_spread_is_still_reported():
    """The gap this nearly had. A fully-closed idea claims none of its symbols, so a
    `positions` row that outlives the close surfaces as a held position no open
    spread explains — rather than being swallowed by the group that closed it."""
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000))
    await store.apply(opening(order_id=1001, direction=OrderDirection.CLOSE))
    assert (await store.open_spreads(today=LEDGER_DAY)).fully_explained, "precondition: cleanly closed"

    # A row survives that the close should have removed.
    store.positions[PUT_SHORT] = -2

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert scan.unattributed == (PUT_SHORT,)
    assert not scan.fully_explained


# -- held to expiry (2026-08-24 evening -> 2026-08-25 morning) -----------------
#
# 0DTE expiry produces NO closing fill — SPXW is cash-settled and European, so the
# exchange closes the position and nothing reaches `FillConsumer`. Until this was
# taught to the scan, an opening order with no close stayed "outstanding" by this
# arithmetic FOREVER, while the `positions` rows it disagreed with were resolved
# away by `reconcile.expiry_verdicts` at the next restart. The result was a
# permanent `attributed != held` divergence reported as `malformed` — one Tier 1
# `watchdog.unwatchable` page and a Tier 2 repeat every 15 seconds, all day, for
# two fully-settled zero-risk positions.
#
# The fix narrows what "outstanding" means for an EXPIRED leg, and nothing else.
# → docs/decisions/2026-08-12-expired-position-resolution.md (2026-08-25 addendum)

NEXT_DAY = date(2026, 7, 30)   # the morning after LEDGER_DAY's expiry


async def test_a_spread_held_to_expiry_simply_leaves_the_scan():
    """The exact reported shape: an opening order, no closing fill, past expiry,
    and the `positions` rows already resolved away by reconciliation.

    It must be treated like any other fully-closed spread — absent, silent, and
    NOT reported. Not `malformed` (nothing is corrupt), and not `unattributed`
    (the legs were legitimately claimed and legitimately closed, by the exchange).
    """
    store = InMemoryOrderStore()
    await store.apply(opening())
    # What the morning reconcile does to a held-to-expiry position: `positions`
    # goes, `orders`/`order_legs` deliberately stay (a P&L history to lose).
    store.positions.clear()
    assert store.orders, "precondition: the order ledger is intact"

    scan = await store.open_spreads(today=NEXT_DAY)
    assert scan.spreads == ()
    assert scan.malformed == ()
    assert scan.unattributed == ()
    assert scan.fully_explained


async def test_the_SAME_ledger_is_still_watched_on_its_own_expiry_day():
    """The safety direction, and the one that would be catastrophic to get wrong.

    The predicate is strictly-before-today with no settlement carve-out, so a live
    0DTE spread is watched for the whole of its own session. Dropping it here
    would take a real position off the watch list with no stop being evaluated.
    """
    store = InMemoryOrderStore()
    await store.apply(opening())

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert len(scan.spreads) == 1
    assert scan.fully_explained


async def test_a_leg_missing_from_positions_for_ANY_OTHER_reason_still_diverges():
    """The test that proves the fix narrowed rather than widened.

    Same disagreement — the ledger claims holdings `positions` does not have — but
    the legs have NOT expired. That is the genuine ghost the divergence check
    exists to catch (a sandbox-reset-retained order ledger, a real bug), and it
    must still be reported exactly as before.
    """
    store = InMemoryOrderStore()
    await store.apply(opening())
    store.positions.clear()

    scan = await store.open_spreads(today=LEDGER_DAY)   # legs expire TODAY, not before
    assert scan.spreads == ()
    assert "disagree with the positions table" in scan.malformed[0]["problem"]


async def test_an_unreadable_leg_symbol_is_never_treated_as_expired():
    """Fail closed, identically to `reconcile.expiry_verdicts`: `occ_expiry` returns
    None for anything it cannot parse, and None is not a past expiry. The leg stays
    outstanding and reaches the divergence check."""
    store = InMemoryOrderStore()
    await store.apply(opening(short="SPXW  P5000", long_="SPXW  P4995"))
    store.positions.clear()

    scan = await store.open_spreads(today=NEXT_DAY)
    assert scan.spreads == ()
    assert "disagree with the positions table" in scan.malformed[0]["problem"]


async def test_an_expired_leg_still_in_positions_is_unattributed_not_malformed():
    """The one state the narrowing moves rather than removes.

    Reconciliation resolves those rows before any session's watchdog scans, so this
    is not reachable in production — but if a `positions` row for an EXPIRED
    contract ever did survive, "a held row no open spread explains" is the true
    statement, and it is the one this now makes. `malformed` would be the wrong
    claim: there is no spread left to be malformed.
    """
    store = InMemoryOrderStore()
    await store.apply(opening())

    scan = await store.open_spreads(today=NEXT_DAY)
    assert scan.spreads == ()
    assert scan.malformed == ()
    assert scan.unattributed == tuple(sorted((PUT_SHORT, PUT_LONG)))


async def test_expiry_awareness_pairs_identically_in_both_stores(db: sqlite3.Connection):
    """The fake must not diverge from production on this case either — every
    watchdog test that uses the in-memory store depends on it."""
    sqlite = await sqlite_store(db)
    memory = InMemoryOrderStore()
    for store in (sqlite, memory):
        await store.apply(opening(order_id=1000))
        await store.apply(
            opening(
                order_id=1001,
                short="SPXW  260817C05100000",     # a LATER expiry: still live
                long_="SPXW  260817C05105000",
                candidate="20260817-call-5100-5105",
            )
        )

    on_the_day = await sqlite.open_spreads(today=LEDGER_DAY)
    assert on_the_day == await memory.open_spreads(today=LEDGER_DAY)
    assert len(on_the_day.spreads) == 2

    after = await sqlite.open_spreads(today=NEXT_DAY)
    assert after == await memory.open_spreads(today=NEXT_DAY)
    # The 0DTE pair expired; the later-dated one is untouched, and its own
    # `positions` rows are all that is left to agree with.
    assert [s.candidate_id for s in after.spreads] == ["20260817-call-5100-5105"]
    assert after.unattributed == tuple(sorted((PUT_SHORT, PUT_LONG)))


# -- the realized entry credit (2026-09-02, migration 0005) -------------------
#
# `entry_credit` was the filled opening order's LIMIT until this. A limit order
# fills at the limit OR BETTER, so the basis ran low and the 2x stop ran tight —
# the safe direction, which is why it survived two months of being known about.
#
# 2026-09-01 gave it a realized cost. `put-7630-7625` recorded 0.45 (the walk's
# final limit) against 0.50 actually collected on both contracts, and stopped at
# 0.925 against a stop of 0.90; on the true basis the stop is 1.00 and it is never
# stopped out at all. Five observations of the gap now exist and every one of them
# is a half-tick or a tick of price improvement.
#
# The trap the tests below are mostly about: A WALK THAT REPRICES FILLS ACROSS
# SEVERAL BROKER ORDERS UNDER ONE IDENTIFIER. 1429812 filled 1 contract and
# 1429813 filled 1, so "the newest filled opening order" is one rung of several.


async def test_the_realized_credit_is_weighted_across_every_rung_of_the_walk():
    """The 2026-09-01 numbers, verbatim: rung 1 at 2.05/1.55, rung 2 at 2.15/1.65.

    Both rungs netted 0.50 that day, which is what made the newest-rung read right
    by luck. The averages differ per leg — 2.10 and 1.60 — so this still fails if
    the aggregation is dropped for either of them.
    """
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1429812, credit="0.50", contracts=1,
                              short_price=2.05, long_price=1.55))
    await store.apply(opening(order_id=1429813, credit="0.45", contracts=1,
                              short_price=2.15, long_price=1.65))

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit == Decimal("0.50")
    assert spread.entry_credit_basis is CreditBasis.REALIZED
    assert spread.entry_limit == Decimal("0.45")     # the newest rung's limit
    assert spread.priced_contracts == 2
    assert spread.contracts == 2


async def test_rungs_at_different_net_credits_are_averaged_not_read_off_the_newest():
    """The case 2026-09-01 did not happen to produce, and the reason this is not a
    reporting nicety: rungs at 0.55 and 0.45 are a spread that collected 0.50, and
    the newest rung alone says 0.45 — the same 10% error, in the same direction."""
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000, credit="0.55", contracts=1,
                              short_price=2.10, long_price=1.55))
    await store.apply(opening(order_id=1001, credit="0.45", contracts=1,
                              short_price=2.10, long_price=1.65))

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit == Decimal("0.50")
    assert spread.entry_limit == Decimal("0.45")


async def test_the_weighting_is_by_CONTRACTS_not_by_rung():
    """Two contracts at 0.50 and one at 0.44 is 0.48, not the 0.47 an unweighted
    mean of the two rungs would give."""
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000, credit="0.50", contracts=2,
                              short_price=2.00, long_price=1.50))
    await store.apply(opening(order_id=1001, credit="0.44", contracts=1,
                              short_price=2.00, long_price=1.56))

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit == Decimal("0.48")
    assert spread.priced_contracts == 3


async def test_one_priced_leg_is_not_half_a_credit():
    """A net credit needs both sides. One leg priced and the other silent is not a
    partial answer — it is no answer, and the limit is what it falls back to."""
    store = InMemoryOrderStore()
    await store.apply(opening(credit="0.85", short_price=2.05, long_price=None))

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit == Decimal("0.85")
    assert spread.entry_credit_basis is CreditBasis.LIMIT
    assert spread.priced_contracts == 0


async def test_a_partly_priced_leg_carries_the_covered_average():
    """The broker booked 2 contracts and priced 1. `fill_price` averages what it
    covered and the store carries that against the whole applied slice — which is
    also exactly what the `order.fill` record says, so the ledger and
    `shadow.outcome`'s reconstruction of the same day cannot disagree."""
    store = InMemoryOrderStore()
    await store.apply(
        opening(credit="0.45", contracts=2, short_price=2.05, long_price=1.55,
                priced_contracts=1)
    )

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit == Decimal("0.50")
    assert spread.entry_credit_basis is CreditBasis.REALIZED
    assert spread.priced_contracts == 2


async def test_a_realized_basis_that_is_not_a_credit_keeps_the_position_WATCHED():
    """The fills say a credit spread was opened for a debit. (2026-09-02)

    It falls back to the LIMIT and stays on the watch list. `malformed` would take
    the credit trigger, the underlying trigger, the FLATTEN_BEFORE_CLOSE, the
    `/status` row AND the concurrency slot off a live, well-formed vertical over a
    price-arithmetic contradiction — and the limit basis is LOW, so the stop it
    leaves behind is TIGHT. The basis says so loudly; it is not a quiet `LIMIT`.
    """
    store = InMemoryOrderStore()
    await store.apply(opening(credit="0.85", short_price=1.55, long_price=2.05))

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.malformed == ()
    (spread,) = scan.spreads
    assert spread.entry_credit == Decimal("0.85")
    assert spread.entry_credit_basis is CreditBasis.LIMIT_FILLS_INCOHERENT
    # The rejected number rides along. A page saying "the fills are incoherent"
    # without saying what they said is a page nobody can act on.
    assert spread.fills_credit == Decimal("-0.50")
    assert spread.priced_contracts == 2


async def test_a_non_positive_LIMIT_is_still_malformed_whatever_the_fills_say():
    """The other guard is untouched and is asked FIRST. `build_order` signs OPEN
    positive, so a negative limit is a corrupt record, and what the legs filled at
    says nothing useful about an order whose own limit cannot be trusted."""
    store = InMemoryOrderStore()
    bad = opening(short_price=2.05, long_price=1.55)
    await store.apply(
        Order(
            direction=OrderDirection.OPEN,
            underlying="SPXW",
            legs=bad.legs,
            limit_price=Decimal("-0.85"),
            external_identifier=bad.external_identifier,
            candidate_id=bad.candidate_id,
            broker_order_id=1000,
            status=OrderStatus.FILLED,
        )
    )
    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.spreads == ()
    assert "is not a credit" in scan.malformed[0]["problem"]


async def test_a_replayed_fill_event_does_not_double_count_the_notional():
    """The stream re-sends an order on every status change. The priced columns ride
    behind the same `filled <= already` return `applied_quantity` does, so a repeat
    contributes nothing to either."""
    store = InMemoryOrderStore()
    order = opening(credit="0.45", short_price=2.05, long_price=1.55)
    await store.apply(order)
    await store.apply(order)
    await store.apply(order)

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit == Decimal("0.50")
    assert spread.priced_contracts == 2
    assert spread.contracts == 2


async def test_a_closing_orders_fills_never_reach_the_entry_credit():
    """The credit is what the OPENING collected. An exit debit blended in would
    read as a spread that collected almost nothing and stop on the next tick."""
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000, contracts=2, credit="0.45",
                              short_price=2.05, long_price=1.55))
    await store.apply(
        opening(order_id=1001, contracts=1, filled=1, credit="0.30",
                direction=OrderDirection.CLOSE, short_price=1.20, long_price=0.90)
    )

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.contracts == 1                     # one closed, one left
    assert spread.entry_credit == Decimal("0.50")    # unchanged by the close
    assert spread.entry_credit_basis is CreditBasis.REALIZED


async def test_chained_spreads_sharing_a_strike_each_keep_their_own_credit():
    """The landmine that governs where this could live at all. `positions` nets the
    shared strike to zero, so nothing symbol-keyed can carry a per-idea price —
    which is why these columns are on `order_legs` and read per trade idea."""
    store = InMemoryOrderStore()
    await store.apply(
        _vertical(order_id=1344929, short=PUT_7750, long_=PUT_7745,
                  candidate="20260817-put-7750-7745", credit="0.60",
                  short_price=3.10, long_price=2.45)          # realized 0.65
    )
    await store.apply(
        _vertical(order_id=1345003, short=PUT_7755, long_=PUT_7750,
                  candidate="20260817-put-7755-7750", credit="0.70",
                  short_price=3.80, long_price=3.05)          # realized 0.75
    )
    assert PUT_7750 not in {s: q for s, q in store.positions.items() if q != 0}

    scan = await store.open_spreads(today=LEDGER_DAY)
    assert scan.fully_explained
    by_candidate = {s.candidate_id: s for s in scan.spreads}
    lower = by_candidate["20260817-put-7750-7745"]
    upper = by_candidate["20260817-put-7755-7750"]
    assert lower.entry_credit == Decimal("0.65")
    assert upper.entry_credit == Decimal("0.75")
    assert lower.entry_credit_basis is upper.entry_credit_basis is CreditBasis.REALIZED
    # The shared strike is one spread's SHORT and the other's LONG, so a cash sign
    # taken from anything but `LegAction.sells` would net them into each other.
    assert lower.short_symbol == upper.long_symbol == PUT_7750


async def test_a_re_entry_blends_the_closed_entrys_credit_and_that_is_the_trade():
    """The one case aggregating across rungs makes WORSE, pinned deliberately.

    Newest-only got this right by accident. It is prevented upstream — `_attempted`
    claims a candidate before `DuplicateOrderGuard` runs, and a re-entry would mint
    the same deterministic `external_identifier` — and it has never been observed,
    while the walk-rung case above is the normal result of any walk that reprices
    and was wrong. If this ever becomes reachable, this test is the one that says
    what to go and fix.
    """
    store = InMemoryOrderStore()
    await store.apply(opening(order_id=1000, credit="0.50",
                              short_price=2.10, long_price=1.60))
    await store.apply(opening(order_id=1001, direction=OrderDirection.CLOSE,
                              short_price=1.00, long_price=0.60))
    await store.apply(opening(order_id=1002, credit="1.20",
                              short_price=3.00, long_price=1.80))

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_limit == Decimal("1.20")     # the newest rung, as before
    assert spread.entry_credit == Decimal("0.85")    # (0.50 + 1.20) / 2, blended
    assert spread.entry_credit_basis is CreditBasis.REALIZED


async def test_the_realized_credit_pairs_identically_in_both_stores(
    db: sqlite3.Connection,
):
    """Migration 0005's columns, through the real UPDATE and the fake's dict, into
    literally the same `_pair_spreads`. Every watchdog test that uses the in-memory
    store is measuring a credit these two must agree on."""
    sqlite = await sqlite_store(db)
    memory = InMemoryOrderStore()
    for store in (sqlite, memory):
        await store.apply(opening(order_id=1429812, credit="0.50", contracts=1,
                                  short_price=2.05, long_price=1.55))
        await store.apply(opening(order_id=1429813, credit="0.45", contracts=1,
                                  short_price=2.15, long_price=1.65))
        # A second idea with no price detail at all, so the LIMIT branch is
        # exercised through both stores in the same read.
        await store.apply(
            _vertical(order_id=1500, short=PUT_7755, long_=PUT_7750,
                      candidate="20260817-put-7755-7750", credit="0.70")
        )

    from_sqlite = await sqlite.open_spreads(today=LEDGER_DAY)
    assert from_sqlite == await memory.open_spreads(today=LEDGER_DAY)
    bases = {s.candidate_id: s.entry_credit_basis for s in from_sqlite.spreads}
    assert bases == {
        "20260729-put-5000-4995": CreditBasis.REALIZED,
        "20260817-put-7755-7750": CreditBasis.LIMIT,
    }


async def test_rows_written_before_the_migration_read_as_the_LIMIT(
    db: sqlite3.Connection,
):
    """The transition contract, and the reason no backfill exists. A row that
    predates 0005 has the column defaults, which is `priced_quantity = 0` — bit for
    bit the behaviour it already had, now labelled."""
    store = await sqlite_store(db)
    await store.apply(opening(credit="0.85", short_price=2.05, long_price=1.55))
    # Exactly what a pre-0005 row looks like once the migration has run.
    db.execute("UPDATE order_legs SET priced_quantity = 0, priced_notional = 0.0")

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit == Decimal("0.85")
    assert spread.entry_credit_basis is CreditBasis.LIMIT
    assert spread.priced_contracts == 0


async def test_the_payload_carries_the_basis_beside_the_number():
    """A stop computed from one number while an operator reads another is its own
    defect, so provenance travels with the credit and never separately."""
    store = InMemoryOrderStore()
    await store.apply(opening(credit="0.45", short_price=2.05, long_price=1.55))
    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads

    payload = spread.to_payload()
    assert payload["entry_credit"] == 0.50
    assert payload["entry_credit_basis"] == "realized"
    assert payload["entry_limit"] == 0.45
    assert payload["fills_credit"] == 0.50
    assert payload["priced_contracts"] == 2
    # `credit_payload()` is the block every other surface splats; the two cannot
    # drift, because `to_payload` is built from it.
    assert spread.credit_payload().items() <= payload.items()


# -- structural: the basis travels with the number ----------------------------


def test_no_module_writes_an_entry_credit_key_by_hand():
    """`credit_payload()` is the only way `entry_credit` reaches a payload.

    `float(spread.entry_credit)` was written by hand into SIX alert and record
    dicts in `watchdog.py`, and on 2026-09-02 a hand-built alert dict is exactly
    what dropped the broker's `reject_reason` from an exit page — the same week,
    one field over. `entry_credit` CHANGED MEANING on 2026-09-02, so one that
    travels without its basis is a number an operator will read as the other one.

    A convention is what failed there. This is the sweep that replaced it, applied
    to the same shape: no dict literal anywhere under `src/argus/` may carry the
    key, except in `store.py`, which builds the block.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src" / "argus"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "store.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "entry_credit":
                    offenders.append(f"{path.relative_to(root)}:{key.lineno}")
    assert not offenders, (
        "these write the key by hand instead of splatting `credit_payload()`, so "
        f"the basis can go missing from them: {offenders}"
    )


def test_the_watchdog_splats_the_credit_block():
    """The other half of the sweep above: absence of the bad shape is not presence
    of the good one. Every page that names a held spread's credit must carry the
    provenance, and there are ten of them (the crash page joined 2026-09-10, the
    profit target's unconfirmed-retire and broker-rejection pages 2026-09-23, the
    stub-close spend record 2026-09-24)."""
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "src" / "argus" / "execution" / "watchdog.py"
    )
    tree = ast.parse(path.read_text())
    splats = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "credit_payload"
    ]
    assert len(splats) == 10, f"expected ten credit-carrying payloads, found {len(splats)}"


async def test_the_ledger_and_the_reporting_path_agree_on_the_realized_credit():
    """`tools.mark` and `tools.desk` reconstruct the same number from `order.fill`.

    They are deliberately NOT refactored into one function: `shadow.outcome` reads
    the AUDIT LOG, which is a record written after the fact, and the ledger reads
    `orders`/`order_legs`, which is the transactional state a live rail must price
    a stop from. Two sources, one arithmetic — so the thing worth pinning is that
    they AGREE, which also catches a divergence in the accumulation that sharing
    the last step would not.

    The fills fed to the reporting path are built from the `AppliedFill`s the store
    itself emits, because that is literally what `fills.py` writes into
    `order.fill` — the same numbers, down the other pipe.
    """
    from datetime import datetime, timezone

    from argus.shadow.outcome import CASH_IN, CASH_OUT, Fill, executions

    store = InMemoryOrderStore()
    applied = []
    applied += list(await store.apply(opening(order_id=1429812, credit="0.50",
                                              contracts=1, short_price=2.05,
                                              long_price=1.55)))
    applied += list(await store.apply(opening(order_id=1429813, credit="0.45",
                                              contracts=1, short_price=2.15,
                                              long_price=1.65)))

    at = datetime(2026, 7, 29, 14, 39, tzinfo=timezone.utc)
    reported = [
        Fill(
            seq=seq,
            at_utc=at,
            candidate_id="20260729-put-5000-4995",
            direction="open",
            symbol=fill.symbol,
            contracts=fill.contracts,
            price=fill.price,
            cash_sign=CASH_IN if fill.action.sells else CASH_OUT,
        )
        for seq, fill in enumerate(applied)
    ]
    execution = executions(reported)["20260729-put-5000-4995"]
    assert execution.problems == ()

    (spread,) = (await store.open_spreads(today=LEDGER_DAY)).spreads
    assert spread.entry_credit_basis is CreditBasis.REALIZED
    assert float(spread.entry_credit) == pytest.approx(execution.entry_credit)
    assert spread.priced_contracts == execution.opened_contracts


async def test_a_leg_that_fills_in_two_events_counts_each_slice_once(
    db: sqlite3.Connection,
):
    """Partial fills are proven behaviour, not a hypothetical: on 2026-08-31 a
    2-leg order booked four leg movements over 3.07 seconds.

    The broker pushes CUMULATIVE `remaining_quantity` and re-sends on every status
    change, so the second event says "2 of 2 filled" and carries both executions.
    `fill_price(already, filled)` prices only the NEW slice, and the columns must
    take that slice's count — taking the cumulative count instead would book the
    first contract twice at the second price and report 3 priced contracts on a
    2-contract position.
    """
    def event(*, filled: int, short_fills, long_fills) -> Order:
        return Order(
            direction=OrderDirection.OPEN,
            underlying="SPXW",
            legs=(
                OrderLeg(PUT_SHORT, LegAction.SELL_TO_OPEN, 2,
                         remaining_quantity=2 - filled, fills=short_fills),
                OrderLeg(PUT_LONG, LegAction.BUY_TO_OPEN, 2,
                         remaining_quantity=2 - filled, fills=long_fills),
            ),
            limit_price=Decimal("0.45"),
            external_identifier="argus-open-20260729-put-5000-4995",
            candidate_id="20260729-put-5000-4995",
            broker_order_id=1000,
            status=OrderStatus.FILLED if filled == 2 else OrderStatus.LIVE,
        )

    first = event(
        filled=1,
        short_fills=(LegFill(quantity=1, price=2.05),),
        long_fills=(LegFill(quantity=1, price=1.55),),
    )
    second = event(
        filled=2,
        short_fills=(LegFill(quantity=1, price=2.05), LegFill(quantity=1, price=2.15)),
        long_fills=(LegFill(quantity=1, price=1.55), LegFill(quantity=1, price=1.65)),
    )

    sqlite = await sqlite_store(db)
    memory = InMemoryOrderStore()
    for store in (sqlite, memory):
        await store.apply(first)
        await store.apply(second)
        await store.apply(second)          # and the stream re-sends it again

    from_sqlite = await sqlite.open_spreads(today=LEDGER_DAY)
    assert from_sqlite == await memory.open_spreads(today=LEDGER_DAY)
    (spread,) = from_sqlite.spreads
    # (2.05 + 2.15)/2 - (1.55 + 1.65)/2 = 2.10 - 1.60
    assert spread.entry_credit == Decimal("0.50")
    assert spread.priced_contracts == 2, "the first contract must not be booked twice"
    assert spread.contracts == 2
