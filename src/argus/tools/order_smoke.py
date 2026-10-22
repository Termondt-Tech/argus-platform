"""Manual sandbox validation of the order primitives.

This is the step that proves the tastytrade SDK behaves the way the docs say it
does — the same role `tools.account` played for the account/reconciliation
primitives before anything stateful was built on top of them. Unit tests run
against `FakeOrderExecution`, and a fake is only ever evidence about the fake.
Specifically, what needs confirming against the real API:

  * a two-leg SPXW vertical is accepted as ONE plain limit order (no complex-order
    API, no per-leg routing), and `Equity Option` really is the instrument type
    tastytrade files SPX index options under;
  * `external_identifier` round-trips, and survives a replace;
  * a replace mints a new order id and links the pair via
    `replaces_order_id`/`replacing_order_id` — the assumption `FakeOrderExecution`
    encodes;
  * `remaining_quantity` is reported per leg.

    ./.venv/bin/python -m argus.tools.order_smoke --config argus.dev.toml
    ./.venv/bin/python -m argus.tools.order_smoke --config argus.dev.toml --live-submit

**Dry run by default.** Without `--live-submit` nothing reaches the sandbox order
book: the order is built and validated (`dry_run=True`) and the full response is
printed. `--live-submit` actually submits, replaces, and cancels once.

**The live path deliberately prices the order where it should not fill** — the
mid plus `--offset` of extra credit, capped below the spread's width. The point
is to exercise submit/replace/cancel, not to acquire a position. A fill is still
possible in a fast market; this is the sandbox, and the tool cancels on the way
out and reports anything left working. It refuses to run against `environment =
"live"` at all.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import time
from datetime import time as dtime
from decimal import Decimal

from tastytrade import Account

from argus.account.rest import BrokerAccount
from argus.bootstrap import configure_logging, load_env
from argus.broker import (
    SessionRole,
    TastytradeSession,
    market_data_credentials,
    trading_credentials,
)
from argus.clock import et_time_of_day, et_today
from argus.config import Config
from argus.execution.broker import TastytradeOrderExecution, to_sdk_order
from argus.execution.build import build_order
from argus.execution.models import Order, OrderAck, OrderDirection
from argus.execution.zerobid import TICK, round_to_tick
from argus.marketdata.dxlink import DXLinkMarketData
from argus.signal.live_chain import DXLinkChainSource
from argus.signal.models import SpreadSignal
from argus.signal.strategy import CreditSpreadStrategy

logger = logging.getLogger("argus.tools.order_smoke")


async def _run(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    if config.environment == "live":
        raise SystemExit("refusing to run the order smoke tool against a live session")

    trading = TastytradeSession(config, trading_credentials(config), role=SessionRole.TRADING)
    md_tt = TastytradeSession(
        config, market_data_credentials(config), role=SessionRole.MARKET_DATA
    )
    print(
        f"trading session: env={config.environment} (is_test={trading.is_test})\n"
        f"market data:     source={config.marketdata.source} "
        f"(is_test={md_tt.is_test}, READ-ONLY)"
    )
    try:
        await trading.open()
        await md_tt.open()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"login failed: {exc}") from exc

    md: DXLinkMarketData | None = None
    try:
        accounts = await Account.get(trading.trading_session)
        if args.account:
            accounts = [a for a in accounts if a.account_number == args.account]
        if not accounts:
            raise SystemExit("no matching account (see `python -m argus.tools.account`)")
        account = accounts[0]
        print(f"account: {account.account_number}\n")

        md = DXLinkMarketData(md_tt.market_data_session, clock=time.monotonic)
        await md.start()
        signal = await _find_spread(md, md_tt.market_data_session, config)

        execution = TastytradeOrderExecution(trading.trading_session, account)
        order = build_order(
            signal,
            direction=OrderDirection.OPEN,
            contracts=args.contracts,
            trading_date=et_today(),
            now=time.monotonic(),
            max_signal_age_s=config.execution.signal_max_age_s,
        )
        _print_built(order, signal)

        await _dry_run(execution, order)
        if args.live_submit:
            broker = BrokerAccount(trading.trading_session, account)
            before = await _print_positions(broker, "BEFORE")
            try:
                await _live_cycle(execution, order, signal, offset=args.offset)
            finally:
                # Flatness is verified AFTER, not just before: a lingering test
                # order that fills leaves a position, and the next session's 09:40
                # reconciliation would refuse to run the day over it.
                after = await _print_positions(broker, "AFTER")
                _report_flatness(before, after)
        else:
            print(
                "\nDry run only. Re-run with --live-submit to exercise "
                "submit/replace/cancel against the sandbox book."
            )
    finally:
        if md is not None:
            await md.stop()
        await md_tt.aclose()
        await trading.aclose()


# -- picking a real spread ----------------------------------------------------


async def _find_spread(
    md: DXLinkMarketData, md_session, config: Config
) -> SpreadSignal:
    """Subscribe the 0DTE chain and select a real two-leg vertical from it.

    The strategy is run twice on purpose. First with the REAL config, so the
    output says whether a genuine entry candidate existed. Then, if it did not,
    with `min_credit` and the entry window relaxed — because this tool validates
    ORDER MECHANICS, and refusing to test them outside 10:00-15:00 or on a
    low-credit day would mean the mechanics only ever get exercised by accident.
    Relaxing the thresholds reuses `CreditSpreadStrategy` rather than
    reimplementing strike selection here, where a second copy could drift from
    the one that actually trades.
    """
    underlying = config.chain.underlying_symbol
    await md.subscribe([underlying])
    print(f"waiting for {underlying} spot ...")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and md.latest(underlying) is None:
        await asyncio.sleep(0.5)
    if md.latest(underlying) is None:
        raise SystemExit(f"no {underlying} quote within 30s")

    chain_source = DXLinkChainSource(
        md,
        underlying_symbol=underlying,
        chain_symbol=config.chain.chain_symbol,
        strike_window_points=config.chain.strike_window_points,
        clock=time.monotonic,
        max_age_s=config.staleness.market_data_max_age_s,
        greeks_max_age_s=config.staleness.greeks_max_age_s,
    )
    n = await chain_source.load(md_session, today=et_today())
    print(f"subscribed {n} 0DTE {config.chain.chain_symbol} contracts")
    if n == 0:
        raise SystemExit("no 0DTE expiry today — nothing to build an order from")

    print("waiting for quotes + greeks ...")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        cov = chain_source.coverage()
        print(
            f"   quotes {cov.with_quote}/{cov.total}   "
            f"greeks {cov.with_greeks}/{cov.total}   "
            f"admissible {cov.admissible}/{cov.total} "
            f"(short-selectable {cov.short_admissible})"
        )
        # `short_admissible`, not `admissible` — unlike the session loop, this tool
        # runs the strategy ONCE and needs a candidate out of it, which needs a
        # short strike, which needs a fresh delta. Waiting on the weaker count
        # would just move the failure a few lines down with a worse message.
        if cov.short_admissible:
            break
    else:
        raise SystemExit(
            "no contract had a FRESH quote and greeks within 30s "
            f"(present-but-stale: quote {cov.stale_quote}, greeks {cov.stale_greeks}; "
            f"priceable {cov.admissible}, short-selectable {cov.short_admissible})"
        )

    snapshot = chain_source.snapshot()
    if snapshot is None:
        raise SystemExit("chain snapshot unavailable")

    now_et = et_time_of_day()
    real = CreditSpreadStrategy(config.signal).evaluate(snapshot, now_et)
    if real.signal is not None:
        print(f"\nreal strategy FIRED: {real.signal.describe()} credit={real.signal.credit:.2f}")
        return real.signal

    print(f"\nreal strategy did not fire (reason: {real.reason}).")
    print("  Relaxing min_credit and the entry window to exercise order mechanics.")
    print("  THIS IS NOT A TRADEABLE CANDIDATE — it is a real two-leg spread to send.")
    relaxed = dataclasses.replace(
        config.signal,
        min_credit=0.0,
        entry_earliest=dtime(0, 0),
        entry_latest=dtime(23, 59),
    )
    result = CreditSpreadStrategy(relaxed).evaluate(snapshot, now_et)
    if result.signal is None:
        raise SystemExit(
            f"could not select any two-leg spread from the chain: {result.reason} "
            f"{result.detail}"
        )
    return result.signal


# -- the steps ----------------------------------------------------------------


def _print_built(order: Order, signal: SpreadSignal) -> None:
    print("\n--- built order (nothing sent) -------------------------------")
    print(f"  {signal.describe()}   width={signal.width:g}  credit={signal.credit:.4f}")
    print(f"  external_identifier: {order.external_identifier}")
    print(f"  candidate_id:        {order.candidate_id}")
    print(f"  limit_price:         {order.limit_price} (positive = credit)")
    for leg in order.legs:
        print(f"  leg: {leg.symbol}  {leg.action}  qty={leg.quantity}  ({leg.instrument_type})")
    sdk = to_sdk_order(order)
    print(f"  wire payload: {sdk.model_dump_json(exclude_none=True, by_alias=True)}")


async def _dry_run(execution: TastytradeOrderExecution, order: Order) -> None:
    print("\n--- dry run (dry_run=True) ----------------------------------")
    ack = await execution.submit(order, dry_run=True)
    _print_ack(ack)
    if ack.errors:
        raise SystemExit(
            "the dry run came back with errors — fix those before submitting for real"
        )


async def _live_cycle(
    execution: TastytradeOrderExecution,
    order: Order,
    signal: SpreadSignal,
    *,
    offset: float,
) -> None:
    print("\n--- LIVE (sandbox book) -------------------------------------")
    submit_price = _unfillable_price(order.limit_price, signal.width, offset)
    print(
        f"  pricing at {submit_price} instead of the {order.limit_price} mid: asking for "
        f"more credit than the market pays, so this should book rather than fill."
    )
    placed: Order | None = None
    working_ids: list[int] = []
    try:
        ack = await execution.submit(order.with_limit_price(submit_price), dry_run=False)
        placed = ack.order
        if placed.broker_order_id is not None:
            working_ids.append(placed.broker_order_id)
        print("\n  [1/6] submit")
        _print_ack(ack)

        print("\n  [2/6] get_live_orders")
        await _print_live(execution)

        print("\n  [3/6] duplicate external_identifier, against a WORKING order")
        await _probe_duplicate(execution, order, submit_price, working_ids)

        # Step further away from the market — still one order, same legs, same
        # external_identifier, new number.
        replace_price = round_to_tick(submit_price + TICK)
        print(f"\n  [4/6] replace at {replace_price}")
        replaced = await execution.replace(
            placed.broker_order_id, placed.with_limit_price(replace_price)
        )
        if replaced.broker_order_id is not None:
            working_ids.append(replaced.broker_order_id)
        _print_order(replaced, indent="    ")
        print(
            f"    replaces_order_id={replaced.replaces_order_id} "
            f"(submitted as {placed.broker_order_id})"
        )
        print(f"    external_identifier round-trip: {replaced.external_identifier}")
        await _print_live(execution)
        placed = replaced

        print(f"\n  [5/6] cancel {placed.broker_order_id}")
        await execution.cancel(placed.broker_order_id)
        await _print_live(execution)

        print("\n  [6/6] duplicate external_identifier, against a TERMINAL order")
        await _probe_duplicate(execution, order, submit_price, working_ids)
    finally:
        # Leave nothing working, whatever went wrong above.
        await _cleanup(execution, working_ids)


async def _probe_duplicate(
    execution: TastytradeOrderExecution,
    order: Order,
    price: Decimal,
    working_ids: list[int],
) -> None:
    """Submit the SAME `external_identifier` a second time and report what happens.

    This is the actual content of the idempotency guarantee `build.py` leans on.
    "Does our identifier format get accepted?" is necessary but not sufficient —
    what matters is whether tastytrade ENFORCES uniqueness, because that decides
    whether `external_identifier` can be trusted as a duplicate-submission guard
    after a crash or only as a label for matching in `get_live_orders()`.

    Both states are worth probing and they can differ:

      * against a WORKING order — the runaway-duplicate case (spec §9: "duplicate/
        runaway order submission"). A rejection here is a free safety rail.
      * against a TERMINAL one — the crash-recovery case. A rejection HERE would be
        a problem rather than a feature: it would block a legitimate re-entry of the
        same spread after a cancelled attempt.

    Any order this creates is registered for cleanup before anything else happens.
    """
    try:
        ack = await execution.submit(order.with_limit_price(price), dry_run=False)
    except Exception as exc:  # noqa: BLE001 — the finding IS the exception
        print(f"    RESULT: raised {type(exc).__name__}: {exc}")
        print("    -> uniqueness appears to be ENFORCED (rejected at the API).")
        return
    if ack.order.broker_order_id is not None:
        working_ids.append(ack.order.broker_order_id)
    if ack.errors:
        print(f"    RESULT: 200 with errors {list(ack.errors)}")
        print("    -> rejected in-band; treat as ENFORCED but check the error code.")
        return
    print("    RESULT: ACCEPTED — a second order exists with the same identifier.")
    print(
        "    -> uniqueness is NOT enforced. external_identifier is a LABEL for "
        "matching, not a broker-side duplicate guard; chunk 2 must check "
        "get_live_orders() BEFORE submitting rather than relying on a rejection."
    )
    _print_ack(ack)


async def _print_positions(broker: BrokerAccount, label: str) -> dict[str, int]:
    positions = await broker.fetch_positions()
    live = {p.symbol: p.signed_quantity for p in positions if p.signed_quantity != 0}
    print(f"\n  positions {label}: {live if live else 'FLAT'}")
    return live


def _report_flatness(before: dict[str, int], after: dict[str, int]) -> None:
    """Confirm the smoke run left no position behind.

    Not a cosmetic check: reconciliation compares broker positions against Argus's
    (still empty) expected set, so ANY position left here refuses the next session
    at 09:40 ET — a lost data day for a test order.
    """
    if after == before:
        print("  account unchanged by this run." + ("" if after else " Flat."))
        return
    print("\n  *** WARNING: THE ACCOUNT CHANGED ***")
    print(f"  before: {before or 'FLAT'}")
    print(f"  after:  {after or 'FLAT'}")
    print(
        "  A test order filled. Close it before the next session — the reconcile\n"
        "  rail compares against an empty expected set and will refuse the day."
    )


async def _cleanup(execution: TastytradeOrderExecution, order_ids: list[int]) -> None:
    answer = await execution.get_live_orders()
    live = {o.broker_order_id: o for o in answer.orders}
    leftovers = [i for i in order_ids if i in live and live[i].is_working]
    # An order of ours we cannot READ still has to be cancelled by hand — it will
    # not appear in `live`, and leaving it working is what the next reconcile
    # hard-halts on.
    _warn_unmappable(answer, order_ids)
    for order_id in leftovers:
        print(f"  cleanup: cancelling still-working order {order_id}")
        try:
            await execution.cancel(order_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  cleanup: FAILED to cancel {order_id}: {exc}")
    remaining = [
        o
        for o in (await execution.get_live_orders()).orders
        if o.is_working and o.broker_order_id in order_ids
    ]
    if remaining:
        print("\n  WARNING: orders still working — check the account:")
        for o in remaining:
            _print_order(o, indent="    ")
    else:
        print("\n  nothing left working.")


def _unfillable_price(mid: Decimal, width: float, offset: float) -> Decimal:
    """The mid plus `offset` of extra credit, kept below the spread's width.

    A credit at or above the width is economically nonsensical (it would imply
    negative max loss) and is the sort of thing a broker rejects, so the cap keeps
    the test order valid while still being priced away from the market.
    """
    ceiling = round_to_tick(width) - TICK
    return min(round_to_tick(mid + Decimal(str(offset))), ceiling)


# -- printing -----------------------------------------------------------------


def _print_ack(ack: OrderAck) -> None:
    print(f"    dry_run={ack.dry_run}  bp_effect={ack.buying_power_effect}  fees={ack.fees}")
    if ack.warnings:
        print(f"    warnings: {list(ack.warnings)}")
    if ack.errors:
        print(f"    ERRORS:   {list(ack.errors)}")
    _print_order(ack.order, indent="    ")


def _print_order(order: Order, *, indent: str = "  ") -> None:
    print(
        f"{indent}id={order.broker_order_id} status={order.status} "
        f"price={order.limit_price} ext_id={order.external_identifier}"
    )
    print(
        f"{indent}replaces_order_id={order.replaces_order_id} "
        f"replacing_order_id={order.replacing_order_id}"
    )
    for leg in order.legs:
        print(
            f"{indent}  leg {leg.symbol} {leg.action} qty={leg.quantity} "
            f"remaining={leg.remaining_quantity} filled={leg.filled_quantity}"
        )


async def _print_live(execution: TastytradeOrderExecution) -> None:
    answer = await execution.get_live_orders()
    print(f"    live orders: {len(answer.orders)}")
    for order in answer.orders:
        _print_order(order, indent="      ")
    _warn_unmappable(answer)


def _warn_unmappable(answer, order_ids: list[int] | None = None) -> None:
    """Print anything the mapper refused, loudly.

    This tool is the manual check that validates the adapter against the real
    sandbox, so an order it cannot map is a FINDING, not noise: it usually means
    tastytrade shipped a status or leg action the mirrored enums do not have, and
    the raw value printed here is the fix. In the running system the same
    condition raises an alert and hard-halts the next reconcile.
    """
    if not answer.unmappable:
        return
    print(f"\n    UNMAPPABLE orders: {len(answer.unmappable)} — the enums are behind")
    for order in answer.unmappable:
        mine = order_ids is not None and order.broker_order_id in order_ids
        print(
            f"      id={order.broker_order_id} status={order.raw_status!r} "
            f"ext={order.external_identifier!r}{'  <- ONE OF OURS' if mine else ''}"
        )
        print(f"        {order.reason}")


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="tastytrade sandbox order smoke tool")
    parser.add_argument(
        "--config",
        required=True,
        help="path to the config file (REQUIRED — there is no default; a default "
        "path is how the live database gets opened by accident)",
    )
    parser.add_argument("--account", default=None, help="account number (default: first)")
    parser.add_argument(
        "--live-submit",
        action="store_true",
        help="actually submit/replace/cancel once against the SANDBOX order book "
             "(default: dry run only, nothing reaches the book)",
    )
    parser.add_argument("--contracts", type=int, default=1, help="order size (default: 1)")
    parser.add_argument(
        "--offset",
        type=float,
        default=1.00,
        help="extra credit above mid for the live order, to keep it from filling "
             "(default: 1.00, capped below the spread width)",
    )
    parser.add_argument("--verbose", action="store_true", help="full SDK DEBUG firehose")
    args = parser.parse_args()
    configure_logging(args.verbose)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
