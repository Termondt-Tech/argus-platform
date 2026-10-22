"""Score a session — the other half of the signal record.

The session records what it would have sent (`shadow.would_submit` in shadow,
`entry.dry_run` in a dry-run environment, `entry.finished` under live fire —
`shadow/marker.py` reads ALL THREE, so the command is the same whichever mode
produced the day and needs no flag to say which). This says what it was WORTH.
Until a day is marked, its records are unscored "yes" answers and `min_credit`
stays a guess.

**On a live day, "worth" splits in two.** A position the watchdog closed intraday
is scored from the prices it actually traded at; one that rode to expiry is scored
against settlement, as everything in shadow always was. The `how` column says
which happened per row, and the footer reports what the stop cost or saved against
holding. See `shadow/outcome.py`.

`--settlement` / `--from-feed` stays required either way. On a day where
everything closed intraday the settlement term is multiplied by zero held
contracts and the number is simply unused — which is cheaper than a second code
path, and much cheaper than an operator having to know in advance which kind of
day it was.

    # after the close, take the settlement off the live feed
    ./.venv/bin/python -m argus.tools.mark --from-feed

    # or supply it (any past date; the only way to mark a half-day or backfill)
    ./.venv/bin/python -m argus.tools.mark --settlement 7480.25
    ./.venv/bin/python -m argus.tools.mark --date 2026-07-20 --settlement 6795.10

    # see what would be scored, write nothing
    ./.venv/bin/python -m argus.tools.mark --settlement 7480.25 --dry-run

`--from-feed` reads the SPX index off the market-data session and refuses to run
before 16:15 ET, because a pre-close quote is not a settlement price. It is still
the *observed index close*, not Cboe's official settlement print, and it assumes
a normal 16:00 session — on a half-day, or for anything that has to be exact,
pass `--settlement`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sqlite3
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from time import monotonic

from argus.bootstrap import configure_logging, load_env
from argus.broker import SessionRole, TastytradeSession, market_data_credentials
from argus.clock import ET, et_now, et_today
from argus.config import Config
from argus.db import apply_migrations, connect
from argus.db.lock import DatabaseLock, DatabaseLockedError
from argus.marketdata.dxlink import DXLinkMarketData
from argus.shadow.marker import mark_trading_date, pending_ideas, pending_scores
from argus.shadow.outcome import (
    RESULT_FULL_CREDIT,
    RESULT_MAX_LOSS,
    SHAPE_CLOSED_INTRADAY,
    SHAPE_PARTLY_CLOSED,
    mark,
)

logger = logging.getLogger("argus.tools.mark")

# A quote before this is not a settlement price. 16:00 plus a buffer for the
# closing index print to settle down. Normal sessions only — see the module
# docstring on half-days.
_EARLIEST_FEED_MARK_ET = time(16, 15)


async def _settlement_from_feed(config: Config, symbol: str, timeout: float) -> float:
    md_tt = TastytradeSession(
        config, market_data_credentials(config), role=SessionRole.MARKET_DATA
    )
    await md_tt.open()
    md = DXLinkMarketData(md_tt.market_data_session, clock=monotonic)
    try:
        await md.start()
        await md.subscribe([symbol])
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            quote = md.latest(symbol)
            if quote is not None and quote.mid is not None:
                return quote.mid
            await asyncio.sleep(0.5)
        raise SystemExit(f"no {symbol} quote within {timeout:g}s")
    finally:
        await md.stop()
        await md_tt.aclose()


# -- display formatting ----------------------------------------------------
#
# Every number in this table is a decimal quantity carried in a binary float, and
# printing one with a plain `.2f` makes the digit depend on float representation
# error rather than on the value. That is not hypothetical here: a credit is a
# HALF-TICK quantity. A leg's floored mid is `(bid + ask) / 2` on a $0.05 ladder,
# so it lands on a $0.025 grid, and so does the net of two legs — meaning roughly
# half of all credits are an ODD multiple of $0.025 (0.525, 0.575, 0.625 ...) and
# sit exactly on the tie that two-decimal rounding has to break.
#
# The float on either side of that tie depends on which leg quotes produced it:
#
#     short 0.60/0.70, long 0.10/0.15  ->  0.5249999999999999  -> "0.52"
#     short 0.65/0.70, long 0.10/0.20  ->  0.525               -> "0.53"
#
# Same arithmetic value, two different printed credits — observed on 2026-07-30
# ranks 17 and 19, in the same run, one row apart. So the fix is two-part:
#
# 1. Round through `Decimal` with an explicit tie rule, after normalizing away
#    the float noise — the same shape (and the same ROUND_HALF_UP convention) as
#    `execution.zerobid.round_to_tick`, which is where this codebase already
#    decided how a price crosses from float economics to an exact decimal.
# 2. Print prices at THREE decimals, not two. Half-tick credits are not
#    two-decimal numbers, and truncating them to two is what made the column
#    fail to reconcile against its own P&L by hand: 0.525 shown as either 0.52
#    or 0.53 gives $208 or $212 on a 4-lot whose P&L really is $210. At three
#    decimals `credit x contracts x 100` is exact for every row — which is the
#    property the column is actually read for. (Three is enough for any real
#    quote: a penny-quoted leg gives a $0.005 grid, a nickel-quoted one $0.025.)
#
# P&L keeps two decimals — it is dollars — but goes through the same rounding so
# a total can never depend on how the float landed either.

_PRICE_PLACES = Decimal("0.001")   # credit / liability: half-tick quantities
_MONEY_PLACES = Decimal("0.01")    # P&L: dollars and cents
# Far finer than any real price increment, far coarser than accumulated float
# noise — so `0.5249999999999999` becomes the 0.525 it was always meant to be
# before any tie has to be broken. Same constant, same reason, as `zerobid`.
_NOISE_FLOOR = Decimal("0.000001")


def _fixed(value: float, places: Decimal) -> Decimal:
    """`value` as an exact decimal at `places`, ties rounding away from zero."""
    if not math.isfinite(value):
        raise ValueError(f"cannot format a non-finite number for display: {value!r}")
    normalized = Decimal(str(value)).quantize(_NOISE_FLOOR, rounding=ROUND_HALF_UP)
    return normalized.quantize(places, rounding=ROUND_HALF_UP)


def _money(value: float | None) -> str:
    """A dollar figure, or `-` when there is honestly no number.

    `None` reaches here only from a position whose fills carried no price detail.
    Printing 0.00 for it would put a fabricated break-even in a column that gets
    summed; the dash is the whole point.
    """
    return "-" if value is None else f"{_fixed(value, _MONEY_PLACES):,f}"


def _format_row(o) -> str:
    """One outcome as a table row. Split out so tests can assert on it."""
    i = o.idea
    return (
        f"{i.entry_rank:>4}  {i.side:<4} "
        f"{i.short_strike:g}/{i.long_strike:g}".ljust(24)
        + f"{_fixed(o.credit_basis, _PRICE_PLACES):>7} {o.scored_contracts:>3} "
        f"{_fixed(o.liability, _PRICE_PLACES):>7} "
        f"{_money(o.pnl):>10}  "
        f"{o.result:<12} {o.shape:<15} "
        f"{i.first_seen_et:%H:%M:%S}-{i.last_seen_et:%H:%M:%S}  {i.proposals:>6}"
    )


def _print_table(outcomes) -> None:
    if not outcomes:
        print("no trade ideas on this date — nothing to score")
        return
    print(
        f"\n{'rank':>4}  {'side':<4} {'strikes':<13} {'credit':>7} {'x':>3} "
        f"{'liab':>7} {'P&L':>10}  {'result':<12} {'how':<15} "
        f"{'window (ET)':<19} {'reprop':>6}"
    )
    print("-" * 116)
    for o in outcomes:
        print(_format_row(o))

    scored = [o for o in outcomes if o.pnl is not None]
    unscored = len(outcomes) - len(scored)
    total = sum(o.pnl for o in scored)
    wins = sum(1 for o in scored if o.win)
    first = [o for o in scored if o.idea.entry_rank == 1]
    print("-" * 116)
    print(
        f"{len(outcomes)} idea(s), {wins} win / {len(scored) - wins} loss, "
        f"total P&L {_money(total)}"
    )
    if unscored:
        # Loud, because this is a position that really traded and whose cost this
        # run could not establish — not a row to skim past.
        print(
            f"!! {unscored} idea(s) UNPRICEABLE and excluded from the total: a real "
            "position whose fills carry no price detail, or whose fill records do "
            "not assemble into a vertical (see execution_problems)"
        )
    # The realistic policy: you'd be in a position after the first fill, so the
    # rank-1 subset is the number that actually means something.
    print(
        f"first-signal-per-side only ({len(first)} idea(s)): "
        f"P&L {_money(sum(o.pnl for o in first))}"
    )
    # LABELLED, because this tally answers "where did the index settle?" and NOT
    # "how did these do". `result` is settlement-derived for every row (see
    # `outcome.RESULT_*`), so for anything closed intraday it is a counterfactual —
    # settlement is not what governed its P&L. Unlabelled, 2026-08-19 printed
    # `full credit 2 ... max loss 0` directly beneath `1 win / 1 loss`, on a day
    # whose second idea was stopped out for -120. Read a week later, that line says
    # "no losers" about a day that had one.
    full = sum(1 for o in outcomes if o.result == RESULT_FULL_CREDIT)
    maxed = sum(1 for o in outcomes if o.result == RESULT_MAX_LOSS)
    print(
        f"where the index settled: full credit {full}  "
        f"partial {len(outcomes) - full - maxed}  max loss {maxed}"
    )
    counterfactual_rows = sum(
        1 for o in outcomes if o.shape in (SHAPE_CLOSED_INTRADAY, SHAPE_PARTLY_CLOSED)
    )
    if counterfactual_rows:
        print(
            f"  ({counterfactual_rows} of these closed before 16:00 — settlement did "
            "not govern P&L; see the realized figure below)"
        )

    # What actually happened, which is a different question from where the index
    # settled — and on a live day the one that decides how each row was scored.
    #
    # The two figures are LIKE FOR LIKE over the same set of positions: what they
    # scored, against what the identical positions would have scored held to 16:00.
    # Comparing realized-on-what-closed against a counterfactual over everything
    # filled would be two different subsets and the delta would mean nothing.
    traded = [o for o in outcomes if o.filled and o.pnl is not None]
    if traded:
        actual = sum(o.pnl for o in traded)
        counterfactual = sum(o.counterfactual_pnl for o in traded)
        closed = sum(1 for o in traded if o.execution.closed_contracts)
        print(
            f"{len(traded)} idea(s) actually filled ({closed} closed intraday): "
            f"scored {_money(actual)}; the same positions held to settlement would "
            f"have been {_money(counterfactual)}"
        )
        # The virtual stop's bill for the day, which is what `credit_multiple` and
        # `underlying_buffer_points` get tuned against.
        delta = actual - counterfactual
        verb = "cost" if delta < 0 else "saved"
        print(f"  -> exiting early {verb} {_money(abs(delta))}")


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Read-only handle for `--dry-run`. Cannot write, so it needs no lock.

    Fails loudly on a missing file rather than silently creating an empty one —
    the default `sqlite3.connect` behaviour, which here would report "0 trade
    ideas" for a typo'd path.
    """
    if not Path(db_path).exists():
        raise SystemExit(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


async def _run(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    trading_date = (
        date.fromisoformat(args.date) if args.date else et_today()
    )

    if args.from_feed:
        now = et_now()
        if trading_date != now.date():
            raise SystemExit(
                f"--from-feed reads the CURRENT index quote; it cannot settle {trading_date}. "
                "Pass --settlement for a past date."
            )
        if now.time() < _EARLIEST_FEED_MARK_ET and not args.force:
            raise SystemExit(
                f"it is {now:%H:%M} ET — a quote before "
                f"{_EARLIEST_FEED_MARK_ET:%H:%M} is not a settlement price. "
                "Wait, pass --settlement, or override with --force."
            )
        settlement = await _settlement_from_feed(
            config, config.chain.underlying_symbol, args.timeout
        )
        source = "feed_close"
        print(f"{config.chain.underlying_symbol} close from feed: {settlement}")
    else:
        settlement = args.settlement
        source = "manual"

    if args.dry_run:
        # READ-ONLY: no lock, no migrations, no writes of any kind. A dry run is
        # therefore safe to do mid-session, against a database another process
        # is actively writing (WAL readers never block the writer).
        conn = _open_readonly(config.audit.db_path)
        try:
            scores = pending_scores(conn, trading_date)
            print(
                f"{trading_date}: {len(scores)} distinct trade idea(s) "
                f"from {sum(i.proposals for i, _ in scores)} proposal record(s)"
            )
            _print_table([mark(i, settlement, ex) for i, ex in scores])
            print("\n(dry run — nothing written)")
        finally:
            conn.close()
        return

    # WRITE PATH. Marking appends shadow.outcome records to the hash chain, which
    # makes this a second writer against the same database as a running session.
    # `seq` is assigned from in-memory state seeded at open, so without this lock
    # marking mid-session steals sequence numbers and wedges that session's audit
    # writer — the 2026-07-21 failure exactly. Held across migrations and the
    # write, then released.
    lock = DatabaseLock(config.audit.db_path)
    try:
        lock.acquire()
    except DatabaseLockedError as exc:
        raise SystemExit(
            f"{exc}\n\n"
            "Marking writes to the audit chain, so it cannot run alongside a live "
            "session. Stop the session and re-run, or use --dry-run to preview "
            "the scoring read-only without writing."
        ) from exc

    conn = connect(config.audit.db_path, synchronous=config.audit.synchronous)
    apply_migrations(conn)
    try:
        ideas = pending_ideas(conn, trading_date)
        print(
            f"{trading_date}: {len(ideas)} distinct trade idea(s) "
            f"from {sum(i.proposals for i in ideas)} proposal record(s)"
        )
        filled = sum(1 for i in ideas if i.proposals == 0)
        if filled:
            # `proposals == 0` means the only evidence of this idea is its fills —
            # a position that really traded with no markable proposal record. See
            # `marker._synthesize`.
            print(
                f"  ({filled} of them reconstructed from fills alone — no markable "
                "proposal record was written for them)"
            )

        # AuditLog is imported lazily so --dry-run never touches the chain.
        from argus.audit.log import AuditLog

        audit = AuditLog(
            conn,
            tier2_queue_maxsize=config.audit.tier2_queue_maxsize,
            tier2_queue_alert_depth=config.audit.tier2_queue_alert_depth,
        )
        try:
            outcomes = await mark_trading_date(
                conn, audit, trading_date, settlement,
                settlement_source=source, remark=args.remark,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        _print_table(outcomes)
        print(f"\nrecorded {len(outcomes)} shadow.outcome record(s) at settlement {settlement}")
    finally:
        conn.close()
        lock.release()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. `argv` is injectable so `tools.eod` can render this
    section in-process rather than shelling out to a second container."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument("--date", help="ET trading date (YYYY-MM-DD); default today")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--settlement", type=float, help="settlement index price")
    source.add_argument(
        "--from-feed", action="store_true", help="read the index close off the live feed"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="--from-feed quote wait")
    parser.add_argument("--dry-run", action="store_true", help="score but write nothing")
    parser.add_argument(
        "--remark", action="store_true",
        help="score a date again (the earlier scoring is NOT removed)",
    )
    parser.add_argument("--force", action="store_true", help="bypass the --from-feed time check")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_env()
    configure_logging(verbose=args.verbose)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
