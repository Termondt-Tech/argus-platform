"""The duplicate-submission guard — the only backstop that exists.

tastytrade does NOT enforce `external_identifier` uniqueness (measured against
the real sandbox, 2026-07-29: the same identifier was accepted twice, both against
a working order and a terminal one). So these tests are not about the API's
behaviour; they are about the three-way classification being applied correctly to
what the API reports, in the one place that decides whether to route an order.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from argus.execution.fake import FakeOrderExecution
from argus.execution.guard import (
    BLOCKED_FILLED,
    BLOCKED_UNMAPPABLE,
    BLOCKED_WORKING,
    DuplicateOrderGuard,
)
from argus.execution.models import (
    LegAction,
    Order,
    OrderDirection,
    OrderLeg,
    OrderStatus,
)

OURS = "argus-open-20260729-put-5000-4995"


def spread(identifier: str = OURS, quantity: int = 1) -> Order:
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg("SPXW  P5000", LegAction.SELL_TO_OPEN, quantity),
            OrderLeg("SPXW  P4995", LegAction.BUY_TO_OPEN, quantity),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
    )


class _Audit:
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
    execution, audit = FakeOrderExecution(), _Audit()
    return DuplicateOrderGuard(execution, audit=audit), execution, audit


# -- the three-way classification, at the call site that matters --------------


async def test_nothing_at_the_broker_allows_the_submission(wiring):
    guard, _, audit = wiring
    verdict = await guard.has_conflicting_order(OURS)

    assert verdict.blocked is False
    assert verdict.reason is None
    assert [c for c, _ in audit.tier2] == ["order.duplicate_check"]


async def test_a_working_order_blocks(wiring):
    guard, execution, audit = wiring
    await execution.submit(spread(), dry_run=False)      # sits at RECEIVED

    verdict = await guard.has_conflicting_order(OURS)

    assert verdict.blocked is True
    assert verdict.reason == BLOCKED_WORKING
    assert verdict.conflicts[0].disposition == "working"
    # A refused submission is a decision not to trade — Tier 1, with the reason.
    assert [c for c, _ in audit.tier1] == ["order.duplicate_blocked"]


async def test_a_filled_order_blocks(wiring):
    guard, execution, _ = wiring
    ack = await execution.submit(spread(), dry_run=False)
    execution.fill(ack.order.broker_order_id)

    verdict = await guard.has_conflicting_order(OURS)

    # FILLED is terminal, and is the single least safe thing to resubmit: a real
    # position exists. A "terminal means safe" rule doubles it.
    assert verdict.blocked is True
    assert verdict.reason == BLOCKED_FILLED


async def test_a_cancelled_order_does_not_block_re_entry(wiring):
    guard, execution, _ = wiring
    ack = await execution.submit(spread(), dry_run=False)
    await execution.cancel(ack.order.broker_order_id)

    verdict = await guard.has_conflicting_order(OURS)

    # This is the crash-recovery / re-entry case. Blocking it would be a defect,
    # not a safety feature — and chunk 1's sandbox run depended on it working.
    assert verdict.blocked is False


@pytest.mark.parametrize("status", [OrderStatus.REJECTED, OrderStatus.EXPIRED,
                                    OrderStatus.REMOVED])
async def test_other_dead_terminal_states_do_not_block(wiring, status):
    guard, execution, _ = wiring
    ack = await execution.submit(spread(), dry_run=False)
    execution.set_status(ack.order.broker_order_id, status)

    assert (await guard.has_conflicting_order(OURS)).blocked is False


async def test_a_partially_filled_then_cancelled_order_blocks(wiring):
    guard, execution, _ = wiring
    ack = await execution.submit(spread(quantity=3), dry_run=False)
    execution.fill(ack.order.broker_order_id, contracts=1)     # 1 of 3
    await execution.cancel(ack.order.broker_order_id)

    verdict = await guard.has_conflicting_order(OURS)

    # Status says CANCELLED; the account says "you hold a 1-lot". The position
    # decides. This is the case a binary terminal check gets wrong.
    assert verdict.blocked is True
    assert verdict.reason == BLOCKED_FILLED


# -- scoping ------------------------------------------------------------------


async def test_a_different_candidates_order_is_not_a_conflict(wiring):
    guard, execution, _ = wiring
    await execution.submit(spread(identifier="argus-open-20260729-call-5200-5205"),
                           dry_run=False)

    assert (await guard.has_conflicting_order(OURS)).blocked is False


async def test_a_working_order_wins_the_reason_over_a_filled_one(wiring):
    guard, execution, _ = wiring
    filled = await execution.submit(spread(), dry_run=False)
    execution.fill(filled.order.broker_order_id)
    await execution.submit(spread(), dry_run=False)   # the API allows the duplicate

    verdict = await guard.has_conflicting_order(OURS)

    assert verdict.blocked is True
    # Both block; the working one is the actionable half (it can be cancelled),
    # so it heads the record.
    assert verdict.reason == BLOCKED_WORKING
    assert len(verdict.conflicts) == 2


# -- an order that cannot be classified at all --------------------------------


async def test_an_unmappable_order_under_our_identifier_blocks(wiring):
    """The second half of C2, in the module that is the only duplicate protection.

    `get_live_orders()` used to drop an order it could not map, so an unmappable
    WORKING order carrying this identifier read here as no order at all and a
    second one went out. There is no safe reading of "the broker holds something
    under your key and Argus cannot say what".
    """
    guard, execution, audit = wiring
    execution.add_unmappable(broker_order_id=4242, external_identifier=OURS)

    verdict = await guard.has_conflicting_order(OURS)

    assert verdict.blocked is True
    assert verdict.reason == BLOCKED_UNMAPPABLE
    (conflict,) = verdict.conflicts
    assert conflict.broker_order_id == 4242
    assert conflict.disposition == "unmappable"
    # The RAW status string and the reason both reach the audit record. Blocking
    # with nothing but `status=None` tells an operator nothing at 10:05.
    assert conflict.status == "Escrowed"
    assert conflict.unmappable_reason
    assert [c for c, _ in audit.tier1] == ["order.duplicate_blocked"]


async def test_an_unmappable_order_outranks_a_working_one_in_the_reason(wiring):
    """An operator told `conflicting_working_order` goes looking for a working
    order and finds something uninterpretable. Naming it up front is the
    difference between a two-minute answer and a twenty-minute one."""
    guard, execution, _ = wiring
    await execution.submit(spread(), dry_run=False)
    execution.add_unmappable(external_identifier=OURS)

    verdict = await guard.has_conflicting_order(OURS)

    assert verdict.reason == BLOCKED_UNMAPPABLE
    assert len(verdict.conflicts) == 2, "both are reported; only the headline changes"


async def test_an_unmappable_order_for_a_different_candidate_is_not_a_conflict(wiring):
    """Identifier-scoped, like every other answer this guard gives.

    An unreadable order belonging to someone else is real and must be dealt with —
    by the alert `AuditedOrderExecution` raises and the halt the next reconcile
    performs — but it is not a reason to refuse THIS candidate.
    """
    guard, execution, _ = wiring
    execution.add_unmappable(external_identifier="argus-open-20260729-call-5200-5205")

    assert (await guard.has_conflicting_order(OURS)).blocked is False


# -- fail closed --------------------------------------------------------------


async def test_an_unreachable_broker_raises_rather_than_allowing(wiring):
    guard, execution, _ = wiring
    execution.fail_next(RuntimeError("connection reset"))

    # An unanswerable question must never read as permission. Returning
    # "no conflict" here would remove the only duplicate protection in the system
    # at exactly the moment it cannot be verified.
    with pytest.raises(RuntimeError):
        await guard.has_conflicting_order(OURS)


async def test_an_empty_identifier_is_a_bug_not_a_conflict(wiring):
    guard, _, _ = wiring
    # An empty key would match every hand-placed order carrying none, and read as
    # a conflict for reasons unrelated to this candidate.
    with pytest.raises(ValueError, match="external_identifier"):
        await guard.has_conflicting_order("")


async def test_the_guard_works_without_an_audit_log():
    # Chunk 3/5 wire this; it must not require an audit log to be constructible.
    guard = DuplicateOrderGuard(FakeOrderExecution())
    assert (await guard.has_conflicting_order(OURS)).blocked is False


def test_the_verdict_has_no_truthiness_shortcut():
    # Callers gate on `.blocked`. Without `__bool__`, an accidental
    # `if verdict:` is a plain object test — but the absence is asserted so
    # nobody adds one and silently inverts a call site's meaning.
    from argus.execution.guard import ConflictVerdict

    assert "__bool__" not in ConflictVerdict.__dict__


# -- the CLOSE quantity rule (2026-09-24) ---------------------------------------
#
# `docs/designs/2026-09-24-quantity-aware-guard-for-close-identifiers.md` §9.1/§9.2.
# A close of `q` contracts is permitted iff nothing is WORKING under the close
# identifier, nothing is unmappable under EITHER identifier, every leg reports a
# `remaining_quantity`, and `closed_filled + q <= opened_filled` per leg — both
# sums from the broker's own fills off the one `get_live_orders()` fetch. Each
# test names the mutant it kills, per the design's §9.2 table.


OURS_OPEN = "argus-open-20260729-put-5000-4995"
OURS_CLOSE = "argus-close-20260729-put-5000-4995"
SHORT, LONG = "SPXW  260729P05000000", "SPXW  260729P04995000"


def open_spread(identifier: str = OURS_OPEN, quantity: int = 2) -> Order:
    return Order(
        direction=OrderDirection.OPEN,
        underlying="SPXW",
        legs=(
            OrderLeg(SHORT, LegAction.SELL_TO_OPEN, quantity),
            OrderLeg(LONG, LegAction.BUY_TO_OPEN, quantity),
        ),
        limit_price=Decimal("0.85"),
        external_identifier=identifier,
    )


def close_spread(identifier: str = OURS_CLOSE, quantity: int = 2) -> Order:
    return Order(
        direction=OrderDirection.CLOSE,
        underlying="SPXW",
        legs=(
            OrderLeg(SHORT, LegAction.BUY_TO_CLOSE, quantity),
            OrderLeg(LONG, LegAction.SELL_TO_CLOSE, quantity),
        ),
        limit_price=Decimal("-1.70"),
        external_identifier=identifier,
    )


async def _filled_open(execution, quantity: int = 2) -> None:
    ack = await execution.submit(open_spread(quantity=quantity), dry_run=False)
    execution.fill(ack.order.broker_order_id)


async def _partial_close(execution, *, filled: int = 1, quantity: int = 2) -> None:
    """A close that filled `filled` of `quantity` per leg, remainder cancelled —
    the stub-leaving shape the walk produces."""
    ack = await execution.submit(close_spread(quantity=quantity), dry_run=False)
    execution.fill(ack.order.broker_order_id, contracts=filled)
    await execution.cancel(ack.order.broker_order_id)


async def test_t1_a_stub_close_of_q_one_is_permitted_with_the_arithmetic(ruled):
    """T1. Opened 2 (one FILLED open order, both legs `remaining_quantity=0`),
    close filled 1 of 2 per leg and was cancelled, nothing working, `q=1`:
    permitted, with the sums and the allowance on the verdict."""
    guard, execution, audit = ruled
    await _filled_open(execution)
    await _partial_close(execution)

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is False
    assert verdict.reason is None
    assert verdict.quantity.to_payload() == {
        "asked": 1,
        "opened_filled": {SHORT: 2, LONG: 2},
        "closed_filled": {SHORT: 1, LONG: 1},
        "allowance": 1,
    }
    # The state the binary rule blocks on, cleared: Tier 1, with the proof.
    assert [c for c, _ in audit.tier1] == ["order.duplicate_close_permitted"]


async def test_t2_an_over_close_is_refused_the_ledger_lag_sentence(ruled):
    """T2. The same world, `q=2`: the inequality fails — this is the ledger lag
    (`q` overstates held) and the double-close in one refusal. Kills the
    "inequality dropped" mutant."""
    guard, execution, audit = ruled
    await _filled_open(execution)
    await _partial_close(execution)

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=2)

    assert verdict.blocked is True
    assert verdict.reason == "close_quantity_exceeded"
    assert verdict.quantity.allowance == 1
    assert [c for c, _ in audit.tier1] == ["order.duplicate_blocked"]


async def test_t3_a_double_close_is_refused(ruled):
    """T3. The close filled 2 of 2; any further `q` exceeds. The other half of
    the "inequality dropped" mutant."""
    guard, execution, _ = ruled
    await _filled_open(execution)
    ack = await execution.submit(close_spread(), dry_run=False)
    execution.fill(ack.order.broker_order_id)

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is True
    assert verdict.reason == "close_quantity_exceeded"
    assert verdict.quantity.allowance == 0


def _fresh_guard() -> tuple[DuplicateOrderGuard, FakeOrderExecution, _Audit]:
    execution, audit = FakeOrderExecution(), _Audit()
    return (
        DuplicateOrderGuard(execution, audit=audit, close_quantity_rule=True),
        execution,
        audit,
    )


@pytest.fixture
def ruled():
    """A guard BUILT WITH the close quantity rule — how the trading session
    builds it while `watchdog.stub_close_enabled` is on."""
    return _fresh_guard()


async def test_t4_the_exact_remainder_is_permited_equality_is_the_working_case(ruled):
    """T4. `closed 1 + q 1 == opened 2` — equality PERMITS. Kills the `<=` -> `<`
    mutant, which would refuse the exact remainder after booking."""
    guard, execution, _ = ruled
    await _filled_open(execution)
    await _partial_close(execution)

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is False
    # And the first close is allowed exactly as the binary rule allows it: with
    # nothing under the close identifier there is no refusal to clear, so the
    # rule does not run at all (review finding 5, 2026-09-24).
    guard2, execution2, _ = _fresh_guard()
    await _filled_open(execution2)
    first = await guard2.has_conflicting_order(OURS_CLOSE, quantity=2)
    assert first.blocked is False
    assert first.quantity is None


async def test_t5_a_dead_looking_close_with_fills_still_counts(ruled):
    """T5. A CANCELLED close order with 1 filled per leg CONTRIBUTES 1 — the
    cancel-after-partial shape. Kills the "DEAD orders skipped from
    `closed_filled`" mutant, which would understate the closed sum and permit an
    over-close."""
    guard, execution, _ = ruled
    await _filled_open(execution, quantity=3)
    await _partial_close(execution, filled=1, quantity=3)     # 1 of 3, cancelled

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=2)

    assert verdict.blocked is False, "closed 1 + q 2 == opened 3"
    assert verdict.quantity.closed_filled == {SHORT: 1, LONG: 1}
    assert verdict.quantity.allowance == 2


async def test_t6_a_replace_chain_sums_to_the_opened_quantity(ruled):
    """T6. The measured 2026-09-01 shape: the original filled 1 (cancelled
    remainder), the replacement carried the REMAINDER and filled 1, position
    held 2 — so `opened_filled` is the SUM across the chain. Kills the
    concatenation mutant that would drop the candidate's dashes and match
    nothing (the candidate here carries three)."""
    guard, execution, _ = ruled
    original = await execution.submit(open_spread(), dry_run=False)
    execution.fill(original.order.broker_order_id, contracts=1)
    replacement = await execution.replace(
        original.order.broker_order_id, open_spread(quantity=1)
    )
    execution.fill(replacement.broker_order_id)
    await _partial_close(execution, filled=1)

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is False
    assert verdict.quantity.opened_filled == {SHORT: 2, LONG: 2}
    assert verdict.quantity.allowance == 1


async def test_t7_an_uneven_partial_binds_on_the_min_per_leg(ruled):
    """T7. Opened (2,2), closed (1,2) — the naked-remainder leg is the one that
    would invert, so the allowance is its 0. Kills the per-leg `min` -> `max`
    mutant."""
    guard, execution, _ = ruled
    await _filled_open(execution)
    ack = await execution.submit(close_spread(), dry_run=False)
    # Fill 1 on the SHORT leg only, 2 on the LONG, then cancel: an uneven
    # partial the equal-fill helper cannot express.
    order = execution.order(ack.order.broker_order_id)
    legs = (
        dataclasses.replace(order.legs[0], remaining_quantity=1),
        dataclasses.replace(order.legs[1], remaining_quantity=0),
    )
    execution._orders[ack.order.broker_order_id] = dataclasses.replace(
        order, legs=legs, status=OrderStatus.CANCELLED
    )

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is True
    assert verdict.reason == "close_quantity_exceeded"
    assert verdict.quantity.closed_filled == {SHORT: 1, LONG: 2}
    assert verdict.quantity.allowance == 0


async def test_t8_a_leg_without_a_remaining_quantity_is_unreadable(ruled):
    """T8. `remaining_quantity is None` anywhere under EITHER identifier:
    `close_quantity_unreadable`, never a silent zero. Kills the "None remaining
    treated as 0" mutant, which would invent capacity."""
    guard, execution, _ = ruled
    await _filled_open(execution)
    await _partial_close(execution)
    # A second, terminal close rung whose legs never reported quantities.
    bare = close_spread(quantity=1)
    execution._orders[4242] = dataclasses.replace(
        bare, broker_order_id=4242, status=OrderStatus.CANCELLED
    )

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is True
    assert verdict.reason == "close_quantity_unreadable"
    assert verdict.quantity is None


async def test_t9_an_unmappable_open_side_order_blocks(ruled):
    """T9. An order Argus cannot read under the sibling OPEN identifier is an
    unknown INPUT — `opened_quantity_unreadable`, a block the binary rule never
    took. Kills the "open-identifier unmappable check removed" mutant."""
    guard, execution, _ = ruled
    await _filled_open(execution)
    await _partial_close(execution)
    execution.add_unmappable(broker_order_id=4242, external_identifier=OURS_OPEN)

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is True
    assert verdict.reason == "opened_quantity_unreadable"
    assert any(c.disposition == "unmappable" for c in verdict.conflicts)


async def test_t10_no_open_orders_visible_cannot_clear_a_filled_close(ruled):
    """T10. Absence is not a verdict: a close identifier with fills but NO open
    order visible permits nothing (`opened_filled = 0`) — the FILLED refusal
    stays, re-reasoned by the arithmetic."""
    guard, execution, _ = ruled
    await _partial_close(execution)      # a close exists; the open side does not

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)
    assert verdict.blocked is True
    assert verdict.reason == "close_quantity_exceeded"
    assert verdict.quantity.allowance == -1


async def test_the_rule_never_refuses_what_the_binary_rule_allows(ruled):
    """Review finding 5 (2026-09-24). The rule only ever CLEARS a
    `conflicting_filled_order` refusal. A first close — nothing under the close
    identifier — is ALLOWED exactly as before, whatever the open side looks
    like: invisible, unreadable, or reporting no fill counts. Those inputs were
    never the binary rule's, and an ordinary stop, backstop or flatten must not
    be refused (and its one attempt spent) over them."""
    # The open side not visible at all (an empty book).
    guard, _, _ = ruled
    empty = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)
    assert (empty.blocked, empty.reason, empty.quantity) == (False, None, None)

    # An opener Argus cannot read.
    guard, execution, _ = _fresh_guard()
    await _filled_open(execution)
    execution.add_unmappable(broker_order_id=4242, external_identifier=OURS_OPEN)
    unreadable = await guard.has_conflicting_order(OURS_CLOSE, quantity=2)
    assert (unreadable.blocked, unreadable.reason) == (False, None)

    # An opener with no per-leg quantities, and a q the opener cannot cover.
    guard, execution, _ = _fresh_guard()
    execution._orders[4343] = dataclasses.replace(
        open_spread(), broker_order_id=4343, status=OrderStatus.FILLED
    )
    bare = await guard.has_conflicting_order(OURS_CLOSE, quantity=5)
    assert (bare.blocked, bare.reason, bare.quantity) == (False, None, None)


async def test_a_guard_built_without_the_rule_is_binary_byte_for_byte(wiring):
    """Review finding 6 (2026-09-24). `watchdog.stub_close_enabled = false`
    builds the guard WITHOUT the rule, so the stub world asked WITH `q` blocks on
    the FILLED disposition exactly as it always has — the flatten override and
    every retry included. The switch rolls back both halves of the design."""
    guard, execution, audit = wiring
    await _filled_open(execution)
    await _partial_close(execution)

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is True
    assert verdict.reason == BLOCKED_FILLED
    assert verdict.quantity is None
    assert [c for c, _ in audit.tier1] == ["order.duplicate_blocked"]


@pytest.mark.parametrize("remaining", [3, -1])
async def test_a_fill_count_outside_the_leg_size_is_unreadable(ruled, remaining):
    """Review finding 7 (2026-09-24). The rule reads `remaining_quantity` in the
    PERMISSIVE direction, so a count that cannot describe a fill — more remaining
    than the leg's size (a negative fill), or a negative remainder (a fill larger
    than the leg) — refuses as unreadable instead of being summed into an
    allowance."""
    guard, execution, _ = ruled
    await _filled_open(execution)
    await _partial_close(execution)
    # On the OPENER, which is FILLED by status and so classifies without reading
    # its counts — the binary verdict stays `conflicting_filled_order` (from the
    # partial close) and only the rule's arithmetic can see the bad number.
    (opener,) = [
        o for o in (await execution.get_live_orders()).orders
        if o.external_identifier == OURS_OPEN
    ]
    execution._orders[opener.broker_order_id] = dataclasses.replace(
        opener,
        legs=tuple(
            dataclasses.replace(leg, remaining_quantity=remaining)
            for leg in opener.legs
        ),
    )

    verdict = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)

    assert verdict.blocked is True
    assert verdict.reason == "close_quantity_unreadable"


def test_the_trading_session_arms_the_rule_from_the_stub_switch():
    """Review finding 6: the ONE production construction of the guard passes
    `close_quantity_rule=config.watchdog.stub_close_enabled` — pinned, because a
    guard built with the default would silently run binary while the watchdog
    continuation was on (every stub close refused), and one built `True` would
    leave the relaxation live under `stub_close_enabled = false`."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "argus"
    calls = []
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "DuplicateOrderGuard"
            ):
                calls.append((path.name, node))
    assert [name for name, _ in calls] == ["pipeline.py"]
    (_, call), = calls
    (kw,) = [k for k in call.keywords if k.arg == "close_quantity_rule"]
    assert ast.unparse(kw.value) == "config.watchdog.stub_close_enabled"


async def test_t11_working_and_unmappable_under_the_close_id_keep_precedence(ruled):
    """T11. The quantity rule never clears the two rules that outrank it: a
    WORKING close still blocks `conflicting_working_order`, an unmappable one
    still blocks `conflicting_unmappable_order` — both WITH `q` in the ask."""
    guard, execution, _ = ruled
    await _filled_open(execution)
    ack = await execution.submit(close_spread(), dry_run=False)   # sits at RECEIVED
    working = await guard.has_conflicting_order(OURS_CLOSE, quantity=1)
    assert working.blocked is True
    assert working.reason == BLOCKED_WORKING
    await execution.cancel(ack.order.broker_order_id)

    guard2, execution2, _ = _fresh_guard()
    await _filled_open(execution2)
    execution2.add_unmappable(external_identifier=OURS_CLOSE)
    unreadable = await guard2.has_conflicting_order(OURS_CLOSE, quantity=1)
    assert unreadable.blocked is True
    assert unreadable.reason == BLOCKED_UNMAPPABLE


async def test_t12_a_close_without_q_keeps_the_binary_rule(ruled):
    """T12. The fail-closed default is the OLD behaviour: the stub world asked
    WITHOUT a quantity blocks on the FILLED disposition, and no quantity block
    is computed. Kills the "quantity defaults to something non-None" mutant."""
    import inspect

    from argus.execution.guard import DuplicateOrderGuard

    signature = inspect.signature(DuplicateOrderGuard.has_conflicting_order)
    assert signature.parameters["quantity"].default is None

    guard, execution, audit = ruled
    await _filled_open(execution)
    await _partial_close(execution)

    verdict = await guard.has_conflicting_order(OURS_CLOSE)

    assert verdict.blocked is True
    assert verdict.reason == BLOCKED_FILLED
    assert verdict.quantity is None
    assert [c for c, _ in audit.tier1] == ["order.duplicate_blocked"]


async def test_t13_an_open_identifier_ignores_the_quantity(ruled):
    """T13. Over-opening is the size rails' question, not the guard's: `q` on an
    OPEN ask changes nothing. Kills the "quantity rule applied to OPEN
    identifiers" mutant."""
    guard, execution, _ = ruled
    await _filled_open(execution)

    verdict = await guard.has_conflicting_order(OURS_OPEN, quantity=99)

    assert verdict.blocked is True, "the FILLED open order still blocks, binary"
    assert verdict.reason == BLOCKED_FILLED
    assert verdict.quantity is None


@pytest.mark.parametrize("quantity", [0, -1])
async def test_t14_a_non_positive_quantity_is_a_caller_bug(ruled, quantity):
    guard, _, _ = ruled
    with pytest.raises(ValueError, match="positive contract count"):
        await guard.has_conflicting_order(OURS_CLOSE, quantity=quantity)


async def test_t15_an_unreachable_broker_raises_with_the_quantity_too(ruled):
    """T15. The burst owns the retry; no submit is reachable without an answered
    verdict, and the question carries `quantity` through it."""
    guard, execution, _ = ruled
    execution.fail_next(RuntimeError("connection reset"))
    with pytest.raises(RuntimeError):
        await guard.has_conflicting_order(OURS_CLOSE, quantity=1)


async def test_t16_the_records_carry_the_arithmetic(ruled):
    """T16. A permitted close is ONE Tier 1 `order.duplicate_close_permitted`
    with the math; a refused one is Tier 1 `order.duplicate_blocked` with the
    typed reason; a plain check stays Tier 2 as today — now carrying the
    quantity block when one was computed."""
    guard, execution, audit = ruled
    await _filled_open(execution)

    plain = await guard.has_conflicting_order(OURS_CLOSE, quantity=2)
    assert plain.blocked is False
    assert [c for c, _ in audit.tier2] == ["order.duplicate_check"]
    # Nothing to clear, so the rule did not run (finding 5).
    assert audit.tier2[0][1]["quantity"] is None
    assert audit.tier2[0][1]["matching_orders"] == 0
