"""Standing leak guards: run the hot paths hard, assert live objects do not grow.

WHY THIS FILE EXISTS. An intra-session event-loop lag ramp ran undetected on the
droplet from at least 2026-07-28 to 08-05 — loop lag climbing from ~0.07s at the
open to a 1.3s mean by 15:00, every session, resetting on restart. It was
gen-2 GC, whose pause is linear in live-object count, driven by something
accumulating. Nothing in CI could have caught it, because nothing in CI ran a
component long enough for accumulation to be visible.

That is the gap these tests close. The suites elsewhere assert BEHAVIOUR over a
handful of calls; these assert a RESOURCE PROPERTY over many thousands, which is
a different class of bug and needs a different kind of test.

The pattern, and it generalizes to any new hot path:

    1. warm up (first-call caches, lazy imports, interned strings)
    2. hammer the component N times at production-like shape — this window is
       NOT measured; it absorbs whatever warm-up the step above did not finish
    3. `gc.collect()`, count tracked objects
    4. hammer it N times again, at the same shape
    5. `gc.collect()`, count again
    6. assert the delta across the SECOND window is bounded

Measuring a second window rather than the first is what makes this stable. A
short warm-up leaves lazy caches still filling, and a single window then charges
the component for the tail of its own warm-up — noise of the same order as the
bound, which is a flaky test rather than a failing one. Growth that is real
appears in every window; warm-up bleed appears only in the first.

The tolerance is per-iteration and deliberately tight but not zero: a few objects
of jitter from the test harness itself is normal, sustained per-iteration growth
is not. At the observed production rate (~150 objects/sec against ~2,400
events/sec) a leak is ~0.06 objects/event — so a bound of 0.01/iteration has an
order of magnitude of headroom and would still have caught it.

These are slower than a unit test by design. They are not slow enough to matter.
"""

from __future__ import annotations

import gc
from datetime import date
from datetime import time as dtime

import pytest

from support import fresh_loss_budget
from argus.audit.log import AuditLog
from argus.marketdata.dxlink import DXLinkMarketData
from argus.orchestrator import SignalLoop
from argus.risk.breaker import DailyLossBreaker
from argus.risk.gate import PreTradeGate
from argus.risk.killswitch import KillSwitch
from argus.risk.models import RiskSnapshot
from argus.signal.chain import FakeChainSource
from argus.signal.models import CALL, PUT, ChainSnapshot, OptionQuote
from argus.signal.strategy import CreditSpreadStrategy
from tests.support import make_config


def tracked_objects() -> int:
    """Live tracked-object count, after settling every generation."""
    for _ in range(3):
        gc.collect()
    return len(gc.get_objects())


def assert_no_growth(before: int, after: int, iterations: int, *, per_iter: float = 0.01) -> None:
    grown = after - before
    allowed = max(200, int(iterations * per_iter))
    assert grown <= allowed, (
        f"live objects grew by {grown:,} over {iterations:,} iterations "
        f"({grown / iterations:+.4f}/iter, allowed {per_iter}/iter). "
        "Sustained growth here becomes a gen-2 GC pause that scales linearly with "
        "the heap — the 2026-08-05 intra-session lag ramp."
    )


def _chain(spot: float = 7750.0, n: int = 60) -> ChainSnapshot:
    options = []
    for i in range(n):
        for right, sign in ((PUT, -1), (CALL, 1)):
            strike = spot + sign * 5.0 * (i + 1)
            options.append(
                OptionQuote(
                    symbol=f"SPXW{int(strike)}{right[0].upper()}",
                    strike=strike, right=right,
                    delta=sign * max(0.01, 0.5 - i * 0.01),
                    bid=0.60, ask=0.70,
                )
            )
    return ChainSnapshot(underlying="SPXW", underlying_price=spot,
                         options=tuple(options), captured_at=0.0)


class _Builder:
    def build(self) -> RiskSnapshot:
        return RiskSnapshot(open_position_count=0, daily_pnl=0.0, account_nlv=100_000.0,
                            market_data_age_s=0.1, account_stream_age_s=0.5,
                            loss_budget=fresh_loss_budget())


# -- the signal loop: ~4 hours of session at 5s cadence ------------------------


async def test_signal_loop_does_not_accumulate(db):
    """strategy -> regime -> gate -> sizing -> audit, 3,000 ticks.

    That is a full trading day's worth of evaluations. Every one of these writes
    Tier 1 records and builds a fresh ChainSnapshot, RiskSnapshot and
    RegimeSnapshot, so if any of them were retained it would show here.
    """
    audit = AuditLog(db, tier2_queue_maxsize=10_000, tier2_queue_alert_depth=8_000)
    audit.start()
    config = make_config()
    killswitch = KillSwitch(audit)
    loop = SignalLoop(
        strategy=CreditSpreadStrategy(config.signal),
        chain_source=FakeChainSource(_chain()),
        snapshot_builder=_Builder(),
        gate=PreTradeGate(
            audit=audit, config=config, killswitch=killswitch,
            breaker=DailyLossBreaker(audit, daily_max_loss=config.risk.daily_max_loss),
        ),
        audit=audit, config=config,
        clock_et=lambda: dtime(12, 0),
        today_et=lambda: date(2026, 8, 5),
    )
    try:
        # DRAINING THE TIER 2 QUEUE BEFORE MEASURING IS LOAD-BEARING, and getting
        # it wrong cost an hour on 2026-08-05. `evaluate_once()` on the no-signal
        # path never awaits, so a bare loop never yields at all and the Tier 2
        # consumer is starved; even one `sleep(0)` per tick is not enough, because
        # the consumer needs several loop iterations per item (chain lock, then
        # `to_thread` for the INSERT). The queue then grows by ~1 per tick and the
        # harness measures a backlog it created itself.
        #
        # Production never sees that: `SignalLoop.run()` sleeps `signal.cadence_s`
        # (5s) between ticks, which is four orders of magnitude more than the
        # consumer needs. Transient queue occupancy is not retention, so the
        # measurement waits for the queue to empty and then counts what is LEFT.
        async def drain() -> None:
            await audit._queue.join()

        # TWO measured windows, and the assertion is on the SECOND one.
        #
        # 200 iterations of warm-up does NOT reach steady state, and measuring
        # from there charges the component for the tail of its own warm-up. That
        # made this test flaky rather than wrong: in a clean Linux container the
        # first window came in anywhere between 78 and 286 objects against a
        # bound of 200 — it failed roughly two runs in five, and failed on the
        # GitHub runner while passing on a warm dev Mac. The second window is
        # dead flat (-45 objects, repeatable to the object across runs), because
        # by then every lazy cache has filled.
        #
        # This is strictly MORE sensitive than measuring from the warm-up point,
        # not a relaxation: real per-iteration growth appears in both windows,
        # while one-time warm-up bleed appears only in the first. The old form
        # could not distinguish the production signature (~0.06 objects/event,
        # ~180 objects here) from its own noise.
        #
        # Keep the windows where they are. Total Tier 2 records written is
        # 200 + 3_000 + 3_000 = 6_200, deliberately under the 8_000
        # `tier2_queue_alert_depth` above — crossing that threshold retains a
        # fixed ~12,100 objects and would swamp the measurement.
        iterations = 3_000

        for _ in range(200):          # warm up
            await loop.evaluate_once()
        await drain()

        for _ in range(iterations):   # window 1: absorbs the rest of the warm-up
            await loop.evaluate_once()
        await drain()
        settled = tracked_objects()

        for _ in range(iterations):   # window 2: steady state, the real measurement
            await loop.evaluate_once()
        await drain()
        after = tracked_objects()
    finally:
        await audit.aclose()

    assert_no_growth(settled, after, iterations)


# -- market data: the highest-volume path in the process ----------------------


def test_market_data_ingest_does_not_accumulate():
    """~480k events — a few minutes of live feed at the real subscription rate.

    `refresh_interval=0.1` across 121 symbols x (Quote+Greeks) delivers up to
    2,420 events/second, so this is by far the biggest allocator in the process
    and the first place a per-event leak would matter.
    """
    pytest.importorskip("tastytrade.dxfeed")
    from tastytrade.dxfeed import Greeks as DxGreeks
    from tastytrade.dxfeed import Quote as DxQuote

    clock = [0.0]
    md = DXLinkMarketData(session=None, clock=lambda: clock[0])
    symbols = [f".SPXW260805C{7000 + 5 * i}" for i in range(121)]

    def quote(symbol: str, i: int) -> object:
        return DxQuote(eventSymbol=symbol, eventTime=0, sequence=i, timeNanoPart=0,
                       bidTime=0, bidExchangeCode="X", bidPrice=0.60 + (i % 3) * 0.05,
                       bidSize=10, askTime=0, askExchangeCode="X", askPrice=0.70, askSize=10)

    def greeks(symbol: str, i: int) -> object:
        return DxGreeks(eventSymbol=symbol, eventTime=0, eventFlags=0, index=i, time=0,
                        sequence=i, price=0.65, volatility=0.12, delta=-0.15,
                        gamma=0.001, theta=-0.5, rho=0.0, vega=0.02)

    for _ in range(20):               # warm up
        for symbol in symbols:
            md._ingest(quote(symbol, 0))
            md._ingest_greeks(greeks(symbol, 0))
    before = tracked_objects()

    frames = 2_000
    for frame in range(frames):
        clock[0] += 0.1
        for symbol in symbols:
            md._ingest(quote(symbol, frame))
            md._ingest_greeks(greeks(symbol, frame))
    after = tracked_objects()

    events = frames * len(symbols) * 2
    assert_no_growth(before, after, events, per_iter=0.001)


# -- the account liveness refresh: ~2,300 dispatches a session ----------------


async def test_account_refresh_dispatch_does_not_accumulate():
    """A new hot path, so it gets a guard in the file that exists for hot paths.

    Moving the refresh to a worker thread (2026-08-26) added ~2,300 dispatches a
    session through a NEW client, a NEW executor, and a NEW exception path. Each
    one submits a future, builds two `httpx2.Request`/`Response` pairs, constructs
    pydantic models, and wraps a `concurrent.futures.Future` in an asyncio one.
    Every one of those is a plausible retainer, and this file exists because a
    slow accumulator on this exact 10-second event went undetected for eight days.

    Half the iterations FAIL, deliberately: the exception path is the one that
    runs during a storm — the condition the whole change is about — and it is
    where a tagged exception, a `_Failure` wrapper, a `__cause__` chain and a
    traceback could each be retained. A guard that only exercised the happy path
    would miss exactly the case that matters.
    """
    # NOT `Recorder` — it retains every request by design, which is the harness
    # leaking, not the refresher. (It measured +14 objects/iteration, and every
    # one of them was a `httpx2.Request` the test itself was holding.)
    from tests.test_account_refresher import _canned, _refresher

    calls = [0]

    def handler(request):
        import httpx2

        calls[0] += 1
        if calls[0] % 4 == 3:  # a 502 on roughly every fourth request
            return httpx2.Response(
                502, json={"error": {"code": "bad_gateway", "message": "Bad Gateway"}}
            )
        return _canned(request)

    refresher = _refresher(handler)

    async def hammer(n: int) -> None:
        for _ in range(n):
            try:
                await refresher.fetch_snapshot()
            except Exception:  # noqa: BLE001 - the failure path is under test too
                pass

    try:
        iterations = 400

        await hammer(100)                 # warm up
        await hammer(iterations)          # window 1: absorbs the rest of the warm-up
        settled = tracked_objects()
        await hammer(iterations)          # window 2: steady state, the measurement
        after = tracked_objects()
    finally:
        await refresher.aclose()

    assert_no_growth(settled, after, iterations)


# -- the quote book itself: bounded by symbol count, never by time ------------


def test_quote_book_is_bounded_by_symbols_not_by_updates():
    """A latest-only cache must stay latest-only. If this ever became append-only
    it would be invisible in behaviour tests and fatal over a session."""
    from argus.marketdata.book import GreeksBook, QuoteBook

    clock = [0.0]
    quotes, greeks = QuoteBook(lambda: clock[0]), GreeksBook(lambda: clock[0])
    symbols = [f"SYM{i}" for i in range(121)]
    for update in range(5_000):
        clock[0] += 0.1
        for symbol in symbols:
            quotes.update(symbol, 0.6, 0.7, 10, 10)
            greeks.update(symbol, -0.15, 0.001, -0.5, 0.02, 0.12)
    assert len(quotes._quotes) == len(symbols)
    assert len(greeks._greeks) == len(symbols)
