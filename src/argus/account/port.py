"""Ports for the account layer.

`AccountStatePort` is the live-state seam the RiskSnapshot builder will consume.
`BrokerAccountPort` is the authoritative-REST seam the reconciler depends on —
narrow on purpose (reconciliation only needs positions), so a fake is trivial.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from argus.account.models import AccountSnapshot, Balances, Position

#: Attributes an `AccountRefreshPort` implementation may set on an exception it
#: raises, so the caller can record WHICH half of the snapshot failed and a
#: COARSE, stable class — without importing the implementation's exception types.
#:
#: `error_type` alone is a third-party class name (`ReadTimeout`, `TastytradeError`,
#: `ConnectError`), and every query written against it stops matching the day the
#: SDK or httpx renames one. That fragility is the whole reason these two exist:
#: `leg` and `error_class` are OURS, and they stay put.
REFRESH_LEG_ATTR = "argus_refresh_leg"
ERROR_CLASS_ATTR = "argus_error_class"


def refresh_leg(exc: BaseException) -> str | None:
    """Which leg an exception was raised on, or None if not attributable."""
    leg = getattr(exc, REFRESH_LEG_ATTR, None)
    return leg if isinstance(leg, str) else None


def declared_error_class(exc: BaseException) -> str | None:
    """The coarse class an implementation declared, or None if it declared none.

    Reads a plain attribute, so an exception TYPE can declare it once as a class
    attribute (`RefreshBusyError`) or an adapter can tag an instance it is
    re-raising (`ThreadedAccountRefresher`).
    """
    kind = getattr(exc, ERROR_CLASS_ATTR, None)
    return kind if isinstance(kind, str) else None


def tag_error(exc: BaseException, *, leg: str | None = None,
              error_class: str | None = None) -> BaseException:
    """Attach `leg`/`error_class` to `exc` and return it, for `raise tag_error(...)`.

    Deliberately does NOT touch the message: `TastytradeError` text is what
    `is_auth_error` matches on and what the existing audit queries read, so it
    must stay byte-identical to what the SDK produced.
    """
    if leg is not None:
        setattr(exc, REFRESH_LEG_ATTR, leg)
    if error_class is not None:
        setattr(exc, ERROR_CLASS_ATTR, error_class)
    return exc


@runtime_checkable
class AccountStatePort(Protocol):
    def snapshot(self) -> AccountSnapshot | None:
        """Latest known account state, or None if nothing has arrived yet."""

    def age(self) -> float:
        """Seconds since the last account event; +inf if none."""

    def is_stale(self, max_age_s: float) -> bool:
        """True if no state, or the last event is older than max_age_s (fail closed)."""


@runtime_checkable
class BrokerAccountPort(Protocol):
    async def fetch_positions(self) -> list[Position]:
        """Authoritative current positions from the broker (REST)."""


@runtime_checkable
class AccountRefreshPort(Protocol):
    """What the liveness refresher needs: ONE full authoritative re-read.

    ONE method, not two, deliberately. The two-method form invited the caller to
    `gather` them — which is what put two concurrent TLS handshakes on the event
    loop every 10 seconds. One call is one attempt, one budget, one connection,
    and the ORDER of the two requests is the implementation's business.

    **IMPLEMENTATIONS OWN THEIR OWN TIMEOUT.** `AlertStreamerAccount.refresh_now`
    no longer wraps this in `asyncio.timeout`: the live implementation runs on a
    worker thread, and a cancelled await abandons a thread that keeps running, so
    the wrapper would bound nothing while leaking a worker per firing. A port that
    can hang without bounding itself wedges the liveness refresh. `refresh_now`
    keeps a backstop at `refresh_interval_s`, which exists to make a broken
    implementation LOUD — it is not the bound.

    Note what this does NOT change: `BrokerAccountPort.fetch_positions` is
    untouched, so reconciliation, the tools, the initial seed and the watchdog's
    book-confirm all keep working exactly as they do. And `BrokerAccount`
    deliberately does not grow a `fetch_snapshot`: it has no bound of its own, so
    it must not structurally satisfy a Protocol whose contract is "bound
    yourself". After 2026-08-26 it cannot be wired as the refresher by accident.
    → docs/designs/2026-08-25-account-refresh-off-event-loop.md
    """

    async def fetch_snapshot(self) -> tuple[list[Position], Balances]: ...
