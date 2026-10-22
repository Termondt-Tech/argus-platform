"""Order execution engine (spec §7).

Layered so every piece is testable without a broker:

    build.py    pure: (SpreadSignal, direction) -> unsent Order
    models.py   Argus's own Order/OrderLeg/OrderStatus domain types
    zerobid.py  the ONE zero-bid floor + tick-rounding implementation
    broker.py   OrderExecutionPort + the tastytrade adapter
    fake.py     FakeOrderExecution — deterministic, for the test suite

**This package's `__init__` deliberately re-exports nothing.** `zerobid` is
imported by `argus.signal.models` (the floor has exactly one implementation and
the signal layer uses it), while `build` imports `argus.signal.models` back — so
any re-export here would run `build` while `signal.models` is half-initialized
and raise ImportError. Import from the submodules explicitly:

    from argus.execution.build import build_order
    from argus.execution.models import Order, OrderDirection

Scope: these are ATOMIC primitives. One submit call in, one response out. The
cancel/replace price-walking loop and its cadence, fill-event consumption, the
pre-send Tier 1 audit write, and position recording all live *above* this layer.
"""
