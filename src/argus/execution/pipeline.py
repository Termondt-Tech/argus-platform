"""The one place the execution stack is assembled (chunk 5).

Everything that touches the broker's `/orders` endpoints in a running Argus goes
through the stack built here, and it exists as a module rather than as a few lines
inside the session body for one reason: **the decorator layering is load-bearing and
getting it wrong fails silently.**

    AuditedOrderExecution( ThrottledOrderExecution( TastytradeOrderExecution ) )
    \\_______ outermost _______/  \\_____ innermost _____/

- **Audit OUTSIDE.** The Tier 1 "I am about to send this" record must be durable
  before the wire regardless of how long the wire waits for a rate-limit slot (spec
  §6). Throttling outside the audit layer would delay the RECORD too, so a process
  killed while queued would have sent nothing and recorded nothing.
- **Throttle INSIDE.** A decorator, not a helper the caller must remember, so the
  governor covers every order request in the process — including the duplicate
  guard's `get_live_orders()` and the recovery path's requery, neither of which has
  ever heard of it.

Both facts are stated in `ratelimit.py`, and both are now enforced by a test rather
than by whoever reads that docstring next.

One stack per process, shared by all four consumers — the entry executor, the price
walking loop it drives, the duplicate guard, and the watchdog's exit executor. Shared
because the rate limiter must be process-wide to mean anything: two limiters at 40
req/s are a 80 req/s ceiling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from argus.alerts import AlertFn
from argus.audit.log import AuditLog
from argus.config import Config
from argus.execution.audited import AuditedOrderExecution
from argus.execution.broker import TastytradeOrderExecution
from argus.execution.guard import DuplicateOrderGuard
from argus.execution.ratelimit import OrderRateLimiter, ThrottledOrderExecution
from argus.execution.store import SqliteOrderStore

logger = logging.getLogger("argus.execution.pipeline")


@dataclass(frozen=True)
class ExecutionStack:
    """The assembled order path plus the collaborators built on top of it.

    Handed around as one object so no caller can construct half of it — an entry
    path with the audited layer but no store, or a guard pointed at the raw adapter
    while the walk goes through the throttle, would both "work" and both be wrong.
    """

    #: What every consumer submits through. Already audited AND throttled.
    execution: AuditedOrderExecution
    #: The orders/order_legs ledger and the only writer of `positions`.
    store: SqliteOrderStore
    #: The duplicate-submission guard, pointed at the SAME stack (so its
    #: `get_live_orders()` reads are audited-transparent and throttled).
    guard: DuplicateOrderGuard
    #: The process-wide request governor. Exposed for `/status`-style inspection:
    #: a non-zero `throttled_count` in a normal session is itself a defect signal.
    limiter: OrderRateLimiter

    @property
    def records_orders(self) -> bool:
        return self.execution.records_orders


def build_execution_stack(
    *,
    session: Any,
    account: Any,
    audit: AuditLog,
    config: Config,
    alert: AlertFn | None = None,
) -> ExecutionStack:
    """Assemble the order path for one trading session.

    `session` must be a TRADING session (`TastytradeSession.trading_session`, which
    refuses to hand over a possibly-live market-data session) and `account` the
    tastytrade `Account` orders route to.

    `alert` is the phone-reachable sink for the one thing this stack can discover
    on its own: an order the broker holds that Argus cannot represent. Optional
    because it degrades to a log line rather than to nothing (`AlertFn` defaults
    to `log_alert`), so a smoke tool with no notifier still runs.

    The store's `run` seam is `AuditLog.write` — the process's ONE writer connection,
    under the same `flock` and the same chain lock as the audit chain itself. A
    private handle here would sidestep the process lock from inside the process it
    protects (see `AuditLog.write`).
    """
    limiter = OrderRateLimiter(config.execution.max_order_requests_per_second)
    store = SqliteOrderStore(audit.write)
    execution = AuditedOrderExecution(
        ThrottledOrderExecution(TastytradeOrderExecution(session, account), limiter),
        audit=audit,
        store=store,
        alert=alert,
    )
    logger.info(
        "execution stack ready: audit -> throttle (%.0f req/s ceiling) -> tastytrade "
        "(account %s)",
        config.execution.max_order_requests_per_second,
        getattr(account, "account_number", "?"),
    )
    return ExecutionStack(
        execution=execution,
        store=store,
        # The guard asks the BROKER through the full stack on purpose: its reads are
        # order requests like any other and belong under the same governor.
        #
        # The CLOSE quantity rule (2026-09-24) is armed by the SAME switch as the
        # watchdog's stub continuation, so `watchdog.stub_close_enabled = false`
        # rolls back both halves: no continuation AND no guard permission past a
        # filled close — which the flatten override and every exit retry would
        # otherwise still reach. The rule only ever clears a refusal, so off is the
        # conservative side.
        guard=DuplicateOrderGuard(
            execution,
            audit=audit,
            close_quantity_rule=config.watchdog.stub_close_enabled,
        ),
        limiter=limiter,
    )


def trades_for_real(config: Config) -> bool:
    """Whether this environment submits orders at all.

    The ONE environment boundary for submission, so the session body and every test
    ask the same question in the same place. Shadow submits NOTHING (spec §10 step 1)
    — not a dry run, not a validation call, nothing — which is why this is a distinct
    question from `[execution] dry_run_submissions`: that flag chooses HOW a
    submitting environment submits, this chooses WHETHER it submits.
    """
    return config.environment != "shadow"


__all__ = ["ExecutionStack", "build_execution_stack", "trades_for_real"]
