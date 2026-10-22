"""The execution stack's decorator layering (chunk 5).

`ratelimit.py` has documented "audit OUTSIDE, throttle INSIDE" since chunk 3, and
until now that was a docstring. It matters because BOTH ways of getting it wrong fail
silently:

  - throttle outside audit -> the Tier 1 pre-send record is delayed along with the
    wire, so a process killed while queued sent nothing AND recorded nothing;
  - either decorator missing -> orders reach the exchange with no audit record or no
    ledger row, and the next restart's order reconciliation hard-halts on an order it
    cannot explain, the morning after.

So the layering is asserted structurally rather than trusted.
"""

from __future__ import annotations

from argus.execution.audited import AuditedOrderExecution
from argus.execution.broker import TastytradeOrderExecution
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.pipeline import build_execution_stack, trades_for_real
from argus.execution.ratelimit import ThrottledOrderExecution
from argus.execution.store import SqliteOrderStore
from tests.support import make_config


class _FakeAudit:
    async def record(self, category, payload):
        pass

    def telemetry(self, category, payload):
        return True

    async def write(self, fn):
        raise AssertionError("no test here should touch the database")


class _Account:
    account_number = "3AD79010"


def _stack():
    return build_execution_stack(
        session=object(), account=_Account(), audit=_FakeAudit(), config=make_config()
    )


def test_the_layering_is_audit_outermost_throttle_innermost():
    stack = _stack()
    # Outermost: the audit decorator, so the Tier 1 write happens before any wait.
    assert isinstance(stack.execution, AuditedOrderExecution)
    inner = stack.execution._execution
    # Then the governor, so the wait falls BETWEEN the record and the send.
    assert isinstance(inner, ThrottledOrderExecution)
    # Then the real adapter, and nothing else in between.
    assert isinstance(inner._execution, TastytradeOrderExecution)


def test_the_stack_records_orders_so_the_walk_will_accept_it():
    """`PriceWalkingLoop` refuses an `AuditedOrderExecution` built without a store
    (chunk 3), because an unrecorded order is unaccounted for at the next restart.
    That refusal is only useful if the production builder actually passes one."""
    stack = _stack()
    assert stack.records_orders is True
    assert isinstance(stack.store, SqliteOrderStore)


def test_the_guard_reads_through_the_same_throttled_stack():
    """The guard's `get_live_orders()` is an order request like any other and belongs
    under the same governor — which is the whole reason the throttle is a decorator
    rather than something the loop calls."""
    stack = _stack()
    assert isinstance(stack.guard, DuplicateOrderGuard)
    assert stack.guard._execution is stack.execution


def test_the_limiter_ceiling_comes_from_config_not_a_literal():
    stack = _stack()
    expected = 1.0 / make_config().execution.max_order_requests_per_second
    assert stack.limiter.min_interval_s == expected


def test_shadow_is_the_only_environment_that_does_not_submit():
    assert trades_for_real(make_config(environment="shadow")) is False
    assert trades_for_real(make_config(environment="sandbox")) is True
    assert trades_for_real(
        make_config(
            environment="live",
            audit={"synchronous": "FULL"},
            broker={"account_number": "5WX00000"},
        )
    ) is True
