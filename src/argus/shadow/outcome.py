"""Shadow outcome marking — what would the candidate actually have been WORTH?

Shadow mode records what it would have submitted. Without a terminal value that
is a pile of unscored "yes" answers: you can count how often the strategy fired
but not whether firing was right, so `min_credit` and the delta target stay
guesses. This module supplies the missing half.

0DTE SPX spreads settle deterministically, which is what makes this cheap and
exact. SPXW is **PM-settled at the closing index value**, so a vertical's
terminal liability is a closed-form function of the settlement price and the two
strikes — no fill simulation, no path dependence, no mark-to-market. (NB: that is
the 16:00 ET close on a normal session; half-days settle at 13:00, and the
index close observed on a quote feed is not the official Cboe settlement print.
Both matter only at the margin, but they are why the settlement price is an
INPUT here rather than something this module goes and decides for itself.)

DEDUPLICATION IS THE WHOLE POINT.
A 5-second cadence re-proposes the same spread all day. One real shadow session
produced 228 `shadow.would_submit` records representing what was, in truth, one
or two trade ideas. Scoring all 228 would report 228 samples of a strategy that
took two positions — correlated noise dressed up as evidence, and the fastest
possible way to tune a threshold onto nothing. `trade_ideas()` collapses
proposals sharing (trading date, side, strikes) into ONE idea, keeping the FIRST
credit — the one actually collectable — alongside the proposal count and the
credit range, so re-proposals become an entry-timing diagnostic instead of fake n.

THE TWO SOURCES HAVE DIFFERENT CADENCES, AND THAT IS FINE.
`shadow.would_submit` is written on every qualifying tick, so a shadow day gives
each idea tens or hundreds of proposals. `entry.dry_run` is written ONCE per
candidate — `EntryExecutor._attempted` claims the spread before the submit, so
every later tick short-circuits to `entry.already_attempted` — so a dry-run day
gives each idea exactly one. Dedup is unaffected: a one-row group's first row is
its first proposal. What DOES degrade is the diagnostic, and only on dry-run
days: `proposals` reads 1 and `best_credit == worst_credit == credit`, because
the re-proposal history was never recorded at this layer. The credit at the
moment of the entry attempt is still the right number — it is the price the
order was actually built at.

`entry_rank` carries the other half of that realism: rank 1 is the first idea of
the day for a side, i.e. the one a real account would have taken before being in
a position. Filtering to rank 1 answers "what if I took the first signal per
side", which is the policy the live loop will actually run.

SETTLEMENT IS THE COUNTERFACTUAL. IT IS NOT ALWAYS THE ANSWER.
Everything above assumes the idea was never filled and would have ridden to
expiry, which is true by construction in shadow and dry-run: nothing is held, so
"what was it worth" can only mean "what would it have been worth". A LIVE day
breaks that assumption in one specific way — the watchdog's virtual stop closes a
position intraday at real prices, and that position has a REALIZED result the
16:00 print says nothing about. On 2026-08-13 the 7825/7830 call spread opened at
0.55 credit and closed at 1.20 debit: -$130 on 2 contracts, against a settlement
that finished the spread full-credit. Scoring it at settlement would have
reported a win on a trade that lost money.

So a scored idea has TWO terms, and every mode is one point in the same formula:

    P&L = realized on the contracts CLOSED  +  settlement on the contracts HELD

`Execution` carries the first term's inputs, assembled from the fill records; the
settlement math above is the second. An idea that never filled has no execution,
both counts are zero, and it falls back to scoring its PROPOSED size at
settlement — which is the pre-existing behaviour, unchanged, for every shadow and
dry-run record ever written. A position closed in full has no second term. A
position held to expiry has no first term. A partial close has both, and needs no
special case to get there.

**Fill prices, never order limit prices.** The 2026-08-13 exit filled at 1.20 net
against a -1.30 limit; scoring the limit would have overstated the loss by $20 on
2 contracts. When the broker reports no price detail at all the honest answer is
that the position is unpriceable — see `Execution.priced`, which propagates a
None rather than substituting a number nobody was filled at.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from argus.clock import ET
from argus.execution.build import parse_candidate_key
from argus.execution.models import OrderDirection
from argus.occ import occ_strike
from argus.risk.models import EntryProposal
from argus.signal.models import CALL, PUT, SPX_MULTIPLIER, SpreadSignal

# Taken from the enum the order path itself signs orders with, never spelled out
# as literals here: these strings are compared against a `direction` that came
# out of an `external_identifier`, so a rename that missed one side would silently
# net every fill into the wrong term.
OPEN, CLOSE = str(OrderDirection.OPEN), str(OrderDirection.CLOSE)

# How the spread finished, relative to the short strike.
RESULT_FULL_CREDIT = "full_credit"  # settled beyond the short strike; kept everything
RESULT_PARTIAL = "partial"          # settled between the strikes
RESULT_MAX_LOSS = "max_loss"        # settled through the long strike

# WHAT ACTUALLY HAPPENED to the idea, which decides how it was scored. Distinct
# from the result constants above: those describe where the index settled, these
# describe whether that settlement is the number that governs.
SHAPE_UNFILLED = "unfilled"                  # never opened — scored counterfactually
SHAPE_CLOSED_INTRADAY = "closed_intraday"    # bought back; realized from fill prices
SHAPE_HELD_TO_EXPIRY = "held_to_expiry"      # opened and expired; settlement governs
SHAPE_PARTLY_CLOSED = "partly_closed"        # some closed, some expired; both terms
SHAPE_UNPRICEABLE = "unpriceable"            # opened, but the fills carry no price

#: Money in is positive. Selling a leg collects premium; buying one pays it.
CASH_IN, CASH_OUT = 1, -1


def settlement_liability(
    side: str, short_strike: float, long_strike: float, settlement: float
) -> float:
    """What the spread owes at settlement, in points. Never negative, never > width.

    A credit spread's liability is the short leg's intrinsic value capped by the
    long leg — which is exactly what makes the risk defined:

        put  (bull put):  clamp(short_strike - settlement, 0, width)
        call (bear call): clamp(settlement - short_strike, 0, width)
    """
    width = abs(long_strike - short_strike)
    if side == PUT:
        intrinsic = short_strike - settlement
    elif side == CALL:
        intrinsic = settlement - short_strike
    else:
        raise ValueError(f"unknown side {side!r}")
    return min(max(intrinsic, 0.0), width)


def classify(liability: float, width: float) -> str:
    if liability <= 0.0:
        return RESULT_FULL_CREDIT
    if liability >= width:
        return RESULT_MAX_LOSS
    return RESULT_PARTIAL


def markable_payload(
    signal: SpreadSignal, proposal: EntryProposal
) -> dict[str, Any]:
    """The `signal` + `proposal` block that makes an audit record SCOREABLE.

    ONE constructor, TWO writers. `shadow.would_submit` (`orchestrator.py`) and
    `entry.dry_run` (`execution/entry.py`) are the terminal records of two
    different environments, and `shadow/marker.py` reads them as one union — so
    the shape has to be identical, not merely similar. It is a function rather
    than a convention because the failure mode of the convention is silent: a
    writer that drifts by one key name produces records that decode to nothing,
    and nobody finds out until a marking run weeks later reports zero ideas.

    That is not hypothetical. `entry.dry_run` shipped WITHOUT this block, and the
    2026-08-11 sandbox flip therefore stopped the signal-quality series dead —
    see `docs/decisions/2026-08-12-markable-dry-run-records.md`.

    Deliberately NOT `EntryProposal.__dict__` or a `to_payload` on the dataclass:
    the keys here are an audit-log wire contract that `marker._decode` reads back,
    and binding them to a dataclass's field names would let a field rename rewrite
    the contract for every historical record's reader at once.
    """
    return {
        "proposal": {
            "symbol": proposal.symbol,
            "contracts": proposal.contracts,
            "max_loss": proposal.max_loss,
        },
        "signal": signal.to_payload(),
    }


@dataclass(frozen=True)
class Proposal:
    """One proposal record (`shadow.would_submit` or `entry.dry_run`), decoded.

    `seq` is the audit chain's own global counter, which is what makes "the FIRST
    proposal for this spread" well-defined even when the two categories are mixed
    on one trading date — see `trade_ideas`.
    """

    seq: int
    at_utc: datetime
    side: str
    short_strike: float
    long_strike: float
    width: float
    credit: float
    contracts: int

    @property
    def at_et(self) -> datetime:
        return self.at_utc.astimezone(ET)

    @property
    def trading_date(self) -> date:
        return self.at_et.date()


@dataclass(frozen=True)
class TradeIdea:
    """Distinct spread proposed on a trading date, with its re-proposal history."""

    trading_date: date
    side: str
    short_strike: float
    long_strike: float
    width: float
    contracts: int
    credit: float          # at the FIRST proposal — what was actually collectable
    best_credit: float
    worst_credit: float
    proposals: int         # how many times the loop re-proposed this same spread
    first_seen_et: datetime
    last_seen_et: datetime
    first_seq: int
    entry_rank: int        # 1 = first idea of the day on this side

    @property
    def max_loss_per_contract(self) -> float:
        return (self.width - self.credit) * SPX_MULTIPLIER


@dataclass(frozen=True)
class Fill:
    """One `order.fill` record — one leg's movement — decoded.

    `cash_sign` is resolved by the reader from the broker's own leg action, not
    inferred here from `direction`: a closing BUY and an opening BUY both pay
    premium, and a module that guessed the sign from open-vs-close would get the
    exit exactly backwards. `direction` comes from the order's
    `external_identifier` instead, and the reader drops any record where the two
    disagree rather than picking one.
    """

    seq: int
    at_utc: datetime
    candidate_id: str
    direction: str           # 'open' | 'close'
    symbol: str              # OCC leg symbol
    contracts: int
    price: float | None      # per-contract premium; None when the broker said nothing
    cash_sign: int           # +1 premium collected, -1 premium paid
    #: The broker's order id, carried for `tools.ledger`'s join to the broker's own
    #: transaction history. Nothing in scoring reads it; `None` only on a record
    #: that somehow lacks the key.
    broker_order_id: int | None = None

    @property
    def at_et(self) -> datetime:
        return self.at_utc.astimezone(ET)


@dataclass(frozen=True)
class Execution:
    """What ACTUALLY happened to one candidate, netted from its fill records.

    Prices are per-contract NET across the two legs, signed the way the rest of
    this codebase signs a spread: `entry_credit` is money collected opening,
    `exit_debit` is money paid closing. Both are `None` when the broker reported
    fills without price detail, which `priced` turns into a refusal to score
    rather than a fabricated number.
    """

    candidate_id: str
    opened_contracts: int
    closed_contracts: int
    entry_credit: float | None
    exit_debit: float | None
    #: The chain sequence of the FIRST fill. Doubles as the anchor seq when this
    #: execution is the only evidence a trade idea existed at all.
    first_seq: int
    first_fill_et: datetime
    last_fill_et: datetime
    #: Why this execution cannot be trusted, if it cannot. Non-empty means the
    #: fill records do not describe a well-formed vertical — carried rather than
    #: raised so a bad row costs its own idea and not the whole day's scoring.
    problems: tuple[str, ...] = ()

    @property
    def expired_contracts(self) -> int:
        return self.opened_contracts - self.closed_contracts

    @property
    def priced(self) -> bool:
        """Every price this execution needs to be scored is present."""
        if self.problems or self.opened_contracts <= 0:
            return False
        if self.entry_credit is None:
            return False
        return self.closed_contracts == 0 or self.exit_debit is not None


def _net_legs(fills: list[Fill]) -> tuple[int, float | None, tuple[str, ...]]:
    """Net one side of a round trip: (contracts, net cash per contract, problems).

    The net is a plain cash sum over the two legs — `cash_sign * price` — so the
    same arithmetic prices an open (positive: a credit collected) and a close
    (negative: a debit paid) with no branch on which one it is.

    Contracts are the MINIMUM across the legs, not the sum and not either leg's
    own count. A vertical is one multi-leg order, so the legs move together and
    the minimum is simply their common count; where they somehow disagree, the
    smaller is the number of complete spreads that actually exist, and rounding a
    leg imbalance up would score contracts that were never a spread.
    """
    if not fills:
        return (0, None, ())
    by_symbol: dict[str, list[Fill]] = {}
    for fill in fills:
        by_symbol.setdefault(fill.symbol, []).append(fill)
    if len(by_symbol) != 2:
        return (0, None, (f"{len(by_symbol)} filled leg(s), expected 2",))

    contracts = min(sum(f.contracts for f in legs) for legs in by_symbol.values())
    if contracts <= 0:
        return (0, None, ("no contracts filled on at least one leg",))

    net = 0.0
    for legs in by_symbol.values():
        if len({f.cash_sign for f in legs}) != 1:
            # One symbol both bought and sold within the same half of the round
            # trip. Whatever that is, it is not a leg of a vertical, and netting
            # it would quietly cancel two real movements against each other.
            return (0, None, ("a leg was both bought and sold in one direction",))
        priced = [f for f in legs if f.price is not None]
        covered = sum(f.contracts for f in priced)
        if not covered:
            # No price detail on this leg at all. `LegFill` is documented as
            # best-effort — it prices a fill, it does not count one — so its
            # absence is a known state, not corruption. Refuse to price the side.
            return (contracts, None, ())
        avg = sum(f.price * f.contracts for f in priced) / covered  # type: ignore[operator]
        net += legs[0].cash_sign * avg
    return (contracts, net, ())


#: Which leg of the vertical a fill moved. Plain strings, not an enum: they exist
#: to be read inside a `problems` message and nothing branches on them but `_role_problems`.
_SHORT, _LONG = "short", "long"

#: Strikes are compared across two encodings — 8 digits of OCC thousandths on one
#: side, `float()` of a `%g`-formatted candidate key on the other. A tolerance far
#: below the smallest increment anything lists keeps that join exact without
#: pretending two float paths must produce identical bits.
_STRIKE_TOLERANCE = 1e-6


def _expected_to_collect(role: str, direction: str) -> bool:
    """Does this leg COLLECT premium, given its role and which half of the trip?

    A credit spread sells the short strike and buys the long one to open, and
    reverses both to close. One exclusive-or, written out rather than tabled
    because the table would have four rows and no more meaning.
    """
    return (role == _SHORT) == (direction == OPEN)


def _leg_role(symbol: str, short_strike: float, long_strike: float) -> str | None:
    """Which leg of the spread `symbol` is: `_SHORT`, `_LONG`, or None for neither.

    The strike comes out of the OCC symbol because an `order.fill` record carries
    nothing else about the instrument — no typed strike, and `candidate_id` NULL by
    construction. See `occ.occ_strike`.
    """
    strike = occ_strike(symbol)
    if strike is None:
        return None
    if math.isclose(strike, short_strike, abs_tol=_STRIKE_TOLERANCE):
        return _SHORT
    if math.isclose(strike, long_strike, abs_tol=_STRIKE_TOLERANCE):
        return _LONG
    return None


def _role_problems(
    fills: list[Fill], direction: str, short_strike: float, long_strike: float
) -> tuple[str, ...]:
    """Does each fill move the leg it claims to, in the direction that leg must move?

    **This replaced a per-record open/close check, and the shape of the replacement
    is the point** (2026-08-25).

    The old check lived in `marker._decode_fill` and compared the broker's
    open/close SUFFIX against the identifier's direction. tastytrade labels a leg
    action relative to THE ACCOUNT'S POSITION, not to Argus's intent, so on a
    chained book — `signal.width = 5.0` against SPX's 5-point ladder makes
    consecutive candidates share a strike — an opening BUY on a strike another live
    spread is already short comes back `Buy to Close` on an `open` order. Nothing
    is wrong; two fields are answering two questions.

    That check DROPPED the fill. On 2026-08-25 it dropped a real 2-contract leg
    (seq 420587): `_net_legs` then saw one leg where it needed two, and a filled
    position was reported as never taken, in the report that IS the signal record.

    Two things changed. The question is now asked about the CASH SIGN and the
    STRIKE, neither of which the broker relabels — an opening short leg collects
    premium whatever the suffix says. And a violation is returned as a PROBLEM
    rather than deleting the record: a rejection that removes evidence is the same
    defect in the other direction, and `Execution.problems` is already the channel
    for "these records do not describe a well-formed vertical".

    Position-independence makes it strictly stronger than what it replaced. It
    still refuses the corruption the old check was reaching for — an exit's fills
    carrying an entry identifier show up as a short leg PAYING premium on an open —
    and it also catches a fill on a strike belonging to no leg of this candidate,
    which the old check could not see. On a chained book one strike is legitimately
    a leg of two different spreads, so "is this fill even mine?" is a real question.
    """
    problems: list[str] = []
    for fill in fills:
        role = _leg_role(fill.symbol, short_strike, long_strike)
        if role is None:
            problems.append(f"{fill.symbol} is neither leg of this spread")
        elif (fill.cash_sign == CASH_IN) != _expected_to_collect(role, direction):
            problems.append(
                f"the {role} leg of the {direction} moved premium the wrong way "
                f"({fill.symbol})"
            )
    # Deduped, order preserved: a walk that fills in slices produces several fills
    # per leg, and one broken leg should read as one problem, not as five.
    return tuple(dict.fromkeys(problems))


def executions(fills: Iterable[Fill]) -> dict[str, Execution]:
    """Group fill records into one `Execution` per candidate, keyed by candidate id.

    Summed across the whole day rather than per order, deliberately: a walk that
    fills in two slices, or a candidate re-entered after a close, produces several
    orders over the same spread, and the question being answered is what that
    spread cost in total on that date.
    """
    by_candidate: dict[str, list[Fill]] = {}
    for fill in sorted(fills, key=lambda f: f.seq):
        by_candidate.setdefault(fill.candidate_id, []).append(fill)

    out: dict[str, Execution] = {}
    for candidate, group in by_candidate.items():
        open_fills = [f for f in group if f.direction == OPEN]
        close_fills = [f for f in group if f.direction == CLOSE]
        opened, entry_net, open_problems = _net_legs(open_fills)
        closed, exit_net, close_problems = _net_legs(close_fills)
        problems = (*open_problems, *close_problems)
        # Which leg each fill actually moved, and whether it moved the right way.
        # The strikes come from the candidate key, which is the only description of
        # the spread an `order.fill` record carries. An unparseable key is itself a
        # problem: without it no fill here can be attributed to a leg at all.
        parsed = parse_candidate_key(candidate)
        if parsed is None:
            problems = (*problems, f"{candidate!r} is not a candidate key")
        else:
            _, _, short_strike, long_strike = parsed
            problems = (
                *problems,
                *_role_problems(open_fills, OPEN, short_strike, long_strike),
                *_role_problems(close_fills, CLOSE, short_strike, long_strike),
            )
        if closed > opened:
            # Closing more than was opened means the fill records do not describe
            # one round trip — most likely two candidates sharing strikes across a
            # restart. Not something to net out and hope.
            problems = (*problems, f"closed {closed} of {opened} opened contract(s)")
        out[candidate] = Execution(
            candidate_id=candidate,
            opened_contracts=opened,
            closed_contracts=closed,
            entry_credit=entry_net,
            exit_debit=None if exit_net is None else -exit_net,
            first_seq=group[0].seq,
            first_fill_et=group[0].at_et,
            last_fill_et=group[-1].at_et,
            problems=problems,
        )
    return out


@dataclass(frozen=True)
class Outcome:
    idea: TradeIdea
    settlement: float
    liability: float
    #: What the idea actually did, when it did anything. `None` is the shadow and
    #: dry-run case and the live case where the walk never filled — all three
    #: score identically, at settlement, on the PROPOSED size.
    execution: Execution | None = None

    @property
    def result(self) -> str:
        return classify(self.liability, self.idea.width)

    @property
    def filled(self) -> bool:
        return self.execution is not None and self.execution.opened_contracts > 0

    @property
    def shape(self) -> str:
        """Which of the five scoring situations this is. See the SHAPE_ constants.

        **`problems` is asked FIRST, before `filled`** (2026-08-25). `_net_legs`
        reports zero contracts alongside its problems — it cannot count complete
        spreads it could not assemble — so a malformed execution used to fall
        through `filled` and land on UNFILLED. That is the one wrong answer
        available here: UNFILLED means "never opened", it scores the idea
        counterfactually at settlement on the PROPOSED size, and it does so
        silently. A position that really traded then reads as one that never did.

        UNPRICEABLE is the honest shape for it. It refuses to score
        (`scoreable`), returns None rather than a number, and `mark` prints it
        under a `!!` line — so the same record that used to disappear into the
        counterfactual bucket now argues for itself.

        An `Execution` only exists where fills exist, and `executions()` attaches a
        problem to every group it cannot turn into contracts, so "problems and no
        contracts" is exactly the malformed set and nothing else.
        """
        ex = self.execution
        if ex is not None and ex.problems:
            return SHAPE_UNPRICEABLE
        if not self.filled:
            return SHAPE_UNFILLED
        assert ex is not None
        if not ex.priced:
            return SHAPE_UNPRICEABLE
        if ex.closed_contracts == 0:
            return SHAPE_HELD_TO_EXPIRY
        if ex.expired_contracts == 0:
            return SHAPE_CLOSED_INTRADAY
        return SHAPE_PARTLY_CLOSED

    @property
    def scoreable(self) -> bool:
        return self.shape != SHAPE_UNPRICEABLE

    @property
    def credit_basis(self) -> float:
        """The entry credit the score is built on.

        The FILL credit once there is one, and the proposed credit otherwise. For
        a position that really opened, what it was proposed at is a forecast and
        what it filled at is the cost basis; scoring the forecast would report a
        P&L nobody could reconcile against the account.
        """
        if self.filled and self.execution is not None and self.execution.entry_credit is not None:
            return self.execution.entry_credit
        return self.idea.credit

    @property
    def scored_contracts(self) -> int:
        """Contracts the score covers: what filled, or what was proposed."""
        if self.filled and self.execution is not None:
            return self.execution.opened_contracts
        return self.idea.contracts

    @property
    def realized_pnl(self) -> float:
        """First term: contracts bought back intraday, at the prices they traded."""
        ex = self.execution
        if not self.scoreable or ex is None or ex.closed_contracts == 0:
            return 0.0
        assert ex.entry_credit is not None and ex.exit_debit is not None
        return (ex.entry_credit - ex.exit_debit) * ex.closed_contracts * SPX_MULTIPLIER

    @property
    def expiry_pnl(self) -> float:
        """Second term: contracts still held at 16:00, against the settlement print."""
        if not self.scoreable:
            return 0.0
        held = (
            self.execution.expired_contracts
            if self.filled and self.execution is not None
            else self.idea.contracts
        )
        return (self.credit_basis - self.liability) * held * SPX_MULTIPLIER

    @property
    def pnl(self) -> float | None:
        """The score. `None` only when a real position's fills carry no prices.

        None rather than 0.0 or a limit-price substitute: a position whose cost
        cannot be established is a hole in the record, and the one thing worse
        than an unscored position is a confidently wrong number for it.
        """
        if not self.scoreable:
            return None
        return self.realized_pnl + self.expiry_pnl

    @property
    def counterfactual_pnl(self) -> float | None:
        """What the idea would have been worth held to settlement, at `credit_basis`.

        For an unfilled idea this IS `pnl`. For a position the stop closed it is
        the diagnostic the virtual stop's parameters get tuned on: the difference
        between this and `pnl` is what the stop cost, or saved, on that trade.
        """
        if not self.scoreable:
            return None
        return (self.credit_basis - self.liability) * self.scored_contracts * SPX_MULTIPLIER

    @property
    def pnl_per_contract(self) -> float | None:
        pnl = self.pnl
        if pnl is None or self.scored_contracts == 0:
            return None
        return pnl / self.scored_contracts

    @property
    def win(self) -> bool | None:
        pnl = self.pnl
        return None if pnl is None else pnl > 0.0

    @property
    def breached(self) -> bool:
        """Settlement finished through the short strike at all.

        A fact about the 16:00 print, not about the trade: a position the stop
        closed at a loss can still show `breached = false`, which is precisely the
        observation that says the stop fired on a move that came back.
        """
        return self.liability > 0.0

    def to_payload(self) -> dict[str, Any]:
        idea, ex = self.idea, self.execution
        return {
            "trading_date": idea.trading_date.isoformat(),
            "settlement": self.settlement,
            "side": idea.side,
            "short_strike": idea.short_strike,
            "long_strike": idea.long_strike,
            "width": idea.width,
            "credit": idea.credit,
            "contracts": idea.contracts,
            "liability": self.liability,
            "pnl_per_contract": self.pnl_per_contract,
            "pnl": self.pnl,
            "result": self.result,
            "win": self.win,
            "breached": self.breached,
            # Entry-timing diagnostics: how many times the same spread was
            # re-proposed, and how much the credit moved while it was available.
            "proposals": idea.proposals,
            "best_credit": idea.best_credit,
            "worst_credit": idea.worst_credit,
            "first_seen_et": idea.first_seen_et.strftime("%H:%M:%S"),
            "last_seen_et": idea.last_seen_et.strftime("%H:%M:%S"),
            "entry_rank": idea.entry_rank,
            "first_seq": idea.first_seq,
            # WHAT ACTUALLY HAPPENED (2026-08-13). Every key below is null on an
            # idea that never filled, which is every record written before live
            # fire — so an existing reader sees its own fields unchanged and the
            # new ones empty, exactly where the old semantics were already right.
            # The two terms are null (not 0.0) whenever the total is null, so a
            # reader can never sum an unpriceable position's components into a
            # number the `pnl` beside them refuses to state.
            "outcome_shape": self.shape,
            "credit_basis": self.credit_basis,
            "scored_contracts": self.scored_contracts,
            "realized_pnl": self.realized_pnl if self.filled and self.scoreable else None,
            "expiry_pnl": self.expiry_pnl if self.filled and self.scoreable else None,
            "counterfactual_pnl": self.counterfactual_pnl if self.filled else None,
            "opened_contracts": ex.opened_contracts if ex is not None else None,
            "closed_contracts": ex.closed_contracts if ex is not None else None,
            "entry_fill_credit": ex.entry_credit if ex is not None else None,
            "exit_fill_debit": ex.exit_debit if ex is not None else None,
            "execution_problems": list(ex.problems) if ex is not None else None,
        }


def mark(
    idea: TradeIdea, settlement: float, execution: Execution | None = None
) -> Outcome:
    return Outcome(
        idea=idea,
        settlement=settlement,
        liability=settlement_liability(
            idea.side, idea.short_strike, idea.long_strike, settlement
        ),
        execution=execution,
    )


def trade_ideas(proposals: Iterable[Proposal]) -> list[TradeIdea]:
    """Collapse re-proposals of the same spread into distinct trade ideas.

    Ordered by first appearance. See the module docstring for why scoring raw
    proposals instead would manufacture sample size that does not exist.

    **`seq` is the ordering, and that is what makes a MIXED day well-defined.**
    Grouping is on (date, side, strikes) with no category in the key, so a spread
    that appears as both `shadow.would_submit` and `entry.dry_run` — possible on
    any date where the process was restarted across an environment change —
    collapses into ONE idea rather than two, and its credit is the earliest
    proposal by chain sequence regardless of which category wrote it. `seq` is
    assigned by the single chain writer under one lock, so it totally orders
    every record in the log; sorting on `at_utc` instead would leave two records
    sharing a timestamp with no defined winner.
    """
    grouped: dict[tuple[date, str, float, float], list[Proposal]] = {}
    for p in sorted(proposals, key=lambda p: p.seq):
        key = (p.trading_date, p.side, p.short_strike, p.long_strike)
        grouped.setdefault(key, []).append(p)

    # entry_rank counts ideas per (date, side) in order of first appearance.
    rank_counter: dict[tuple[date, str], int] = {}
    ideas: list[TradeIdea] = []
    for (trading_date, side, short_strike, long_strike), group in grouped.items():
        first, last = group[0], group[-1]
        credits = [p.credit for p in group]
        rank_key = (trading_date, side)
        rank_counter[rank_key] = rank_counter.get(rank_key, 0) + 1
        ideas.append(
            TradeIdea(
                trading_date=trading_date,
                side=side,
                short_strike=short_strike,
                long_strike=long_strike,
                width=first.width,
                contracts=first.contracts,
                credit=first.credit,
                best_credit=max(credits),
                worst_credit=min(credits),
                proposals=len(group),
                first_seen_et=first.at_et,
                last_seen_et=last.at_et,
                first_seq=first.seq,
                entry_rank=rank_counter[rank_key],
            )
        )
    return ideas


def rerank(ideas: Iterable[TradeIdea]) -> list[TradeIdea]:
    """Recompute `entry_rank` over a merged set of ideas, ordered by `first_seq`.

    Needed because ideas can now arrive from two places — a proposal record, or
    (when no proposal record survives) the fills of a position that really opened.
    Rank means "the Nth idea of the day on this side", which is a property of the
    whole day's set, so it cannot be assigned by whichever reader happened to
    produce the idea.

    A no-op on `trade_ideas` output, which already ranks in `first_seq` order —
    asserted by a test, so this can be applied unconditionally without a caller
    having to know whether it is needed.
    """
    ordered = sorted(ideas, key=lambda i: i.first_seq)
    counter: dict[tuple[date, str], int] = {}
    out: list[TradeIdea] = []
    for idea in ordered:
        key = (idea.trading_date, idea.side)
        counter[key] = counter.get(key, 0) + 1
        out.append(replace(idea, entry_rank=counter[key]))
    return out
