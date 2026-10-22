"""DXLinkChainSource — the live ChainSource (instruments API + DXLink).

Two phases:

1. `load()` — fetch the option chain, pick today's expiry (0DTE), bound it to a
   strike window around spot, and subscribe those streamer symbols for Quote +
   Greeks. Requires an underlying quote first, because the window is relative to
   spot; it raises rather than guessing a center.

2. `snapshot()` — join subscribed contracts with their live quote (and greeks,
   where those are fresh too) into a `ChainSnapshot`. A contract with no usable
   market is **skipped**, not defaulted: inventing a market is exactly the kind of
   silent fallback that produces a confidently wrong trade.

**Present is not the same as current, and this source once conflated them.**
Until 2026-08-21 the only admission test here was `is None`, so a leg that quoted
once at 09:31 and never again was still joined into every snapshot for the rest of
the day, at any age. Nothing downstream caught it: the pre-trade gate's
`market_data_age_s` covers `reference_symbols`, which is the UNDERLYING alone, and
`execution.signal_max_age_s` measures snapshot-to-order-build latency (~0 by
construction), not quote age. So a wing whose last print predated a widening got
priced, sized, gated and submitted — and `_assert_credit_matches_legs` passed,
because it recomputes the credit from the same stale legs. Self-consistent, not
correct. A leg's QUOTE is now bounded by `staleness.market_data_max_age_s`, the
same bound the exit path's `LegQuotes` applies per leg and the gate applies to the
underlying.

**Admission is ROLE-SCOPED (2026-08-24), because the two legs of a vertical are
chosen by different things.** The 2026-08-21 gate demanded a fresh quote AND fresh
greeks of every leg, and that was right for exactly one of the two: the SHORT
strike is picked BY delta magnitude (`strategy._select`), so a stale delta there
does not degrade an entry, it silently selects a different strike. The LONG leg is
picked by STRIKE DISTANCE and priced off its own market — it contributes no delta
to anything, and delta is the only greek Argus reads. Gating it on greeks bought
no safety and cost the whole ladder: measured over the first live-quote RTH session
(2026-08-24), `signal.skip` fired on 95.7% of loop iterations and every greek in
the 120-symbol chain was stale on 95% of heartbeat samples, while a chain with
every quote stale never occurred once, all-time. So `_admit` now takes a fresh
QUOTE as admission and attaches greeks only when they are fresh too; a leg without
them reaches the strategy with `delta=None`, priceable but never selectable as a
short strike. **This widened what is ADMITTED and nothing about what is TRADEABLE
without a real delta** — that refusal simply moved to the first point at which a
leg HAS a role, which is short-strike selection itself.

Rejection costs a leg its place in the snapshot, not the tick: `strategy._select`
takes the nearest-delta leg among those admitted, so a stale strike is routed
around rather than blocking the day. Only an entirely unquoted chain yields `None`,
which `SignalLoop` records as an ordinary `signal.skip`.

`chain_fetcher` is injectable so the whole class is testable without a broker;
only the real fetch and subscribe touch the network.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from tastytrade.instruments import get_option_chain

from argus.marketdata.book import Clock
from argus.marketdata.models import OptionGreeks, Quote
from argus.marketdata.port import MarketDataPort
from argus.signal.contracts import (
    OptionContract,
    filter_strike_window,
    map_option,
    select_expiration,
)
from argus.signal.models import ChainSnapshot, OptionQuote
from argus.staleness import is_stale

logger = logging.getLogger("argus.signal.chain")

ChainFetcher = Callable[[Any, str], Awaitable[dict[date, list[Any]]]]


@dataclass(frozen=True)
class ChainCoverage:
    """How much of the subscribed chain actually has data.

    Quotes and greeks are counted SEPARATELY on purpose. A single "usable" number
    can't distinguish "no market" from "no delta" — and those have completely
    different causes (dead feed vs. missing options entitlement). Diagnosing that
    from one ambiguous zero is guesswork.

    TWO admission counts, because admission is role-scoped (2026-08-24):

    - `admissible` — legs with a fresh QUOTE. This is the count that equals what
      `snapshot()` will actually emit, and the promise is unchanged from when it
      was the only one: it is the ONE number that cannot drift from the legs the
      strategy is handed, because `coverage()` and `snapshot()` share `_admit`.
    - `short_admissible` — the SUBSET of those that also have fresh greeks, and so
      may be selected as a SHORT strike. `short_admissible <= admissible` always.

    Reading them together is the diagnosis. `admissible` at zero is a quote
    problem and means no signal can be built at all. `admissible` high with
    `short_admissible` at zero is a GREEKS problem: the ladder is fully priceable,
    the strategy just has no delta to pick a short strike by, and it reads as
    `no_short_leg_with_fresh_delta` rather than as a skipped tick. Collapsing
    those two into one number is what hid the 2026-08-24 starvation for a session.

    Both are reported alongside the presence counts rather than replacing them,
    because the gaps ARE the measurement: `with_quote - admissible` is exactly what
    the bound costs on the quote feed, and `stale_quote` / `stale_greeks` attribute
    the rest. (`with_greeks - short_admissible` is only an upper bound on the greeks
    feed's cost — it also absorbs legs whose QUOTE is what failed.) Without these,
    a gate that quietly starved entries would look identical to a day with no
    signal, which is precisely what happened on 2026-08-24.
    """

    total: int
    with_quote: int
    with_greeks: int
    with_both: int
    admissible: int
    short_admissible: int
    stale_quote: int
    stale_greeks: int
    #: Freshest greeks reading anywhere in the chain, in seconds; +inf when no
    #: contract has one at all. The instrument that sizes the NEXT bound: it is a
    #: measure of the FEED, not of the gate, so unlike `short_admissible` it keeps
    #: reporting once the bound is wide enough to admit everything. Reading it
    #: alongside `signal.none` is how "what would bound B have bought" gets
    #: answered from live traffic rather than re-derived from a sampled duty cycle
    #: (which is what disagreed by 2-3x on 2026-08-27).
    min_greeks_age_s: float = math.inf


@dataclass(frozen=True)
class AdmittedLeg:
    """One leg that cleared `_admit`: a fresh quote, and greeks only if fresh.

    A distinct type rather than a `(quote, greeks | None)` tuple, because the
    difference between "not admitted" (`_admit` returns None) and "admitted with
    no usable delta" (`greeks is None`) is the entire point of the role split, and
    two `None`s one unpack apart is how that distinction gets lost.
    """

    quote: Quote
    #: `None` means the greeks are absent or older than the bound. The leg is
    #: still priceable — it just cannot be a SHORT strike.
    greeks: OptionGreeks | None
    #: Age of the greeks reading behind this leg, WHETHER OR NOT it cleared the
    #: bound (+inf when the leg has never had one). Carried separately from
    #: `greeks` on purpose: `greeks is None` says the verdict, this says by how
    #: much, and only the second one can size the next bound. It travels with the
    #: leg for the same reason `delta` does — `strategy.evaluate` is pure over its
    #: snapshot and must not reach back into the market port to ask.
    greeks_age_s: float = math.inf

    @property
    def delta(self) -> float | None:
        return None if self.greeks is None else self.greeks.delta

    @property
    def short_selectable(self) -> bool:
        return self.greeks is not None


class DXLinkChainSource:
    def __init__(
        self,
        market: MarketDataPort,
        *,
        underlying_symbol: str,
        chain_symbol: str,
        strike_window_points: float,
        clock: Clock,
        max_age_s: float,
        greeks_max_age_s: float,
        chain_fetcher: ChainFetcher = get_option_chain,
    ) -> None:
        self._market = market
        # Spot comes from the INDEX (e.g. SPX); the chain comes from the option
        # ROOT (e.g. SPXW). Using the index for both returns only monthlies and
        # yields an empty 0DTE chain.
        self._underlying = underlying_symbol
        self._chain_symbol = chain_symbol
        self._window = strike_window_points
        self._clock = clock
        #: Per-leg QUOTE freshness bound, `staleness.market_data_max_age_s` in
        #: production. REQUIRED with no default: a default is how one of the two call
        #: sites ends up ungated, and an ungated entry path is the bug this argument
        #: exists to close. It is deliberately the SAME bound the exit path and the
        #: pre-trade gate use — one feed, one definition of too old to PRICE against.
        self._max_age_s = max_age_s
        #: Per-leg GREEKS bound, `staleness.greeks_max_age_s` in production, and
        #: deliberately NOT the same number (2026-08-27). A quote is a PRICE and 2s
        #: is right for one. A delta is a SELECTOR, read by exactly one `min()`, and
        #: holding it to a price-grade bound starved short-strike selection to 2.8%
        #: of in-window ticks against a greeks feed whose bursts are ~137s apart.
        #: REQUIRED with no default for the identical reason as above. See
        #: `StalenessConfig.greeks_max_age_s` for what a stale delta actually costs —
        #: a fraction of one strike, and never a price.
        self._greeks_max_age_s = greeks_max_age_s
        self._fetch = chain_fetcher
        self._contracts: tuple[OptionContract, ...] = ()

    @property
    def contracts(self) -> tuple[OptionContract, ...]:
        return self._contracts

    def spot(self) -> float | None:
        """Underlying mid, or None if the underlying hasn't quoted yet."""
        quote = self._market.latest(self._underlying)
        return None if quote is None else quote.mid

    async def load(self, session: Any, *, today: date) -> int:
        """Fetch + filter + subscribe today's 0DTE chain. Returns contract count."""
        spot = self.spot()
        if spot is None:
            raise RuntimeError(
                f"no underlying quote for {self._underlying} yet — subscribe to it and "
                "wait for a quote before loading the chain (the strike window is "
                "centered on spot)"
            )
        chain_by_date = await self._fetch(session, self._chain_symbol)
        todays = select_expiration(chain_by_date, today)
        if not todays:
            logger.warning(
                "no options expiring %s for %s (no 0DTE today?)", today, self._chain_symbol
            )
            self._contracts = ()
            return 0

        mapped = [c for c in (map_option(o) for o in todays) if c is not None]
        windowed = filter_strike_window(mapped, spot, self._window)
        self._contracts = tuple(sorted(windowed, key=lambda c: (c.right, c.strike)))
        if self._contracts:
            await self._market.subscribe([c.streamer_symbol for c in self._contracts])
        logger.info(
            "loaded %d 0DTE %s contracts (spot %.2f from %s, window +/-%.0f)",
            len(self._contracts), self._chain_symbol, spot, self._underlying, self._window,
        )
        return len(self._contracts)

    def _admit(self, streamer_symbol: str) -> AdmittedLeg | None:
        """THE leg-admission rule, role-scoped: a fresh QUOTE admits; fresh GREEKS
        decide whether the admitted leg may be a SHORT strike.

        Still ONE definition, called by both `snapshot()` and `coverage()`, so the
        counts the operator reads can never drift from the legs the strategy is
        handed. The split is inside this rule, not a second rule beside it —
        forking it is how `admissible` starts meaning something `snapshot()` does
        not do.

        **The two roles no longer share a THRESHOLD either (2026-08-27).** 2026-08-24
        split which FIELDS a leg must clear for which job and deliberately kept one
        number, on the reasoning that the number was not the problem. Measurement
        said otherwise: the greeks feed refreshes the chain in synchronized bursts
        ~137s apart (p90 421s), so a 2s bound left a short strike selectable on 2.8%
        of in-window ticks with the ladder fully priceable throughout. The quote
        bound is a PRICE bound and is untouched; the greeks bound is a SELECTOR
        bound and is its own key. Raising `market_data_max_age_s` was never on the
        table — it also governs exit pricing and the gate's staleness rail.

        Staleness comes from `argus.staleness.is_stale` — via the port's own
        `is_stale` for quotes, and applied to `greeks_age` for greeks, which the
        port exposes for exactly this. Fail-closed follows for free: +inf (never
        seen), NaN, and a clock that went backwards all read as stale, which here
        means "no delta", which means "cannot be the short leg".
        """
        quote = self._market.latest(streamer_symbol)
        if quote is None or self._market.is_stale(streamer_symbol, self._max_age_s):
            return None
        greeks_age = self._market.greeks_age(streamer_symbol)
        greeks = self._market.greeks(streamer_symbol)
        # The age is attached whether or not it cleared, so the rejected case stays
        # measurable — `greeks is None` is the verdict, `greeks_age_s` is by how
        # much, and only the second can size the next bound.
        if greeks is None or is_stale(greeks_age, self._greeks_max_age_s):
            return AdmittedLeg(quote, None, greeks_age)
        return AdmittedLeg(quote, greeks, greeks_age)

    def coverage(self) -> ChainCoverage:
        """Per-feed data coverage across subscribed contracts (diagnostic)."""
        with_quote = with_greeks = with_both = 0
        admissible = short_admissible = stale_quote = stale_greeks = 0
        min_greeks_age = math.inf
        for contract in self._contracts:
            symbol = contract.streamer_symbol
            has_quote = self._market.latest(symbol) is not None
            has_greeks = self._market.greeks(symbol) is not None
            with_quote += has_quote
            with_greeks += has_greeks
            with_both += has_quote and has_greeks
            # Counted only where a reading EXISTS, so these attribute the gate's
            # cost to a lagging feed and never double-count a contract that simply
            # has no data yet — that case is already visible as with_quote/greeks.
            #
            # `stale_greeks` measures against the GREEKS bound, not the quote one:
            # this number is what the operator reads to decide whether the bound is
            # right, and measuring it against a threshold the code no longer applies
            # is how the 15s line starts describing a gate that isn't there.
            greeks_age = self._market.greeks_age(symbol)
            stale_quote += has_quote and self._market.is_stale(symbol, self._max_age_s)
            stale_greeks += has_greeks and is_stale(greeks_age, self._greeks_max_age_s)
            if has_greeks and greeks_age < min_greeks_age:
                min_greeks_age = greeks_age
            # Both counts off the SAME `_admit` call, so they cannot disagree with
            # each other or with `snapshot()`.
            admitted = self._admit(symbol)
            admissible += admitted is not None
            short_admissible += admitted is not None and admitted.short_selectable
        return ChainCoverage(
            total=len(self._contracts),
            with_quote=with_quote,
            with_greeks=with_greeks,
            with_both=with_both,
            admissible=admissible,
            short_admissible=short_admissible,
            stale_quote=stale_quote,
            stale_greeks=stale_greeks,
            min_greeks_age_s=min_greeks_age,
        )

    def snapshot(self) -> ChainSnapshot | None:
        spot = self.spot()
        if spot is None or not self._contracts:
            return None
        options: list[OptionQuote] = []
        for contract in self._contracts:
            admitted = self._admit(contract.streamer_symbol)
            if admitted is None:
                # No market, or one too old to price against. Either way there is
                # no price to build a leg from at all, so skip it.
                continue
            options.append(
                OptionQuote(
                    symbol=contract.symbol,
                    strike=contract.strike,
                    right=contract.right,
                    # None when the greeks behind this leg are missing or stale.
                    # `strategy._select` refuses such a leg as a SHORT strike and
                    # accepts it as a long one — the role split lands there because
                    # this is the last point at which no leg has a role yet.
                    delta=admitted.delta,
                    bid=admitted.quote.bid,
                    ask=admitted.quote.ask,
                    # Travels with the leg WHETHER OR NOT the delta cleared, so the
                    # strategy can report how far behind the greeks feed was on a
                    # tick it rejected — the measurement that sizes the next bound.
                    greeks_age_s=admitted.greeks_age_s,
                )
            )
        if not options:
            return None
        return ChainSnapshot(
            underlying=self._chain_symbol,
            underlying_price=spot,
            options=tuple(options),
            captured_at=self._clock(),
        )
