"""The supervisor wires the configured account number through, structurally.

Modelled on `test_credential_slots.py`: the property is that a plausible-looking
tidy cannot silently restore the `accounts[0]` lottery. A behavioural test cannot
reach it — `__main__.main()` runs the supervisor — so it is pinned on the AST
instead.

→ docs/designs/2026-09-10-pinning-the-traded-account.md §4.3 (C9)
"""

from __future__ import annotations

import ast
import pathlib

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "src" / "argus" / "__main__.py"


def test_main_passes_the_configured_account_number():
    """`Application(config)` in `__main__` is the whole of C9's defect.

    The key, the guard and both filters all existed before 2026-09-10; the
    supervisor simply never passed one, so a pinned live config would have loaded
    cleanly, rendered its number on /status, and traded accounts[0] anyway. The
    failure is SILENT in every environment and INVISIBLE in cert, where accounts[0]
    is the right answer — so it is pinned structurally rather than left to review.
    """
    tree = ast.parse(_MAIN.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Application"
    ]
    assert calls, "no Application(...) call found in __main__.py"
    (call,) = calls
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    assert "account_number" in kwargs, (
        "Application is built with no account_number keyword — the supervisor is "
        "back to the accounts[0] lottery: whichever account the login happens to "
        "list first, with a pinned config loaded, rendered on /status, and ignored. "
        "Pass config.broker.account_number explicitly (C9, 2026-09-10)."
    )
    # `ast.unparse` compared as a string, deliberately brittle to renames: a rename
    # should surface HERE first, because the supervisor trading an account other
    # than the pinned one is the C9 defect itself.
    assert ast.unparse(kwargs["account_number"]) == "config.broker.account_number", (
        "the account_number argument is not config.broker.account_number — if the "
        "spelling genuinely changed, update this assertion deliberately; do not "
        "let the supervisor derive the account from anywhere but the config it "
        "just loaded"
    )
