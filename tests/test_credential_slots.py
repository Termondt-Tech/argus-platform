"""Trading credentials are environment-keyed, structurally.

The behavioural coverage lives in `test_session.py`. This file holds the two
STRUCTURAL guarantees — the ones that stop the property being re-broken by a
plausible-looking line in a module nobody thought to re-read.

→ docs/decisions/2026-09-09-trading-credentials-are-environment-keyed.md
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "argus"

#: The module that is ALLOWED to name a slot literally — it is where the selectors
#: live, and where the constants are declared.
_SELECTOR_MODULE = _SRC / "broker" / "session.py"


def _python_files() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_module_builds_credentials_without_naming_the_slot():
    """`TastytradeCredentials.from_env()` with no arguments is banned in `src/`.

    Until 2026-09-09 a bare `from_env()` WAS the trading path, at all seven call
    sites. Once the trading path became environment-keyed, that same line became
    the bug — cert credentials under a live profile, silently, at whichever call
    site was missed or added later. Removing the defaults makes it a `TypeError`;
    this makes it a test failure with an explanation attached, which is what a
    future author actually needs.

    The rule is deliberately about ARGUMENT COUNT, not about which module calls it:
    `from_env` is legitimately reachable from the two selectors and from nowhere
    else, but a third selector would be a reasonable thing to add one day, and it
    would still have to name its slot.
    """
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "from_env":
                continue
            if not node.args and not node.keywords:
                offenders.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert not offenders, (
        "`TastytradeCredentials.from_env()` called with no slot named:\n  "
        + "\n  ".join(offenders)
        + "\nUse `trading_credentials(config)` or `market_data_credentials(config)`, "
        "which key the slot on the same config authority `is_test` keys the endpoint "
        "on. A bare `from_env()` reads the cert pair in every environment — that is "
        "the defect 2026-09-09 removed."
    )


def test_only_the_selector_module_reads_a_credential_variable_from_the_environment():
    """Nothing outside `broker/session.py` may reach `os.environ` for a slot.

    The selectors are the single seam where "which world am I in?" becomes "which
    variable do I read?". A module that reads `TASTYTRADE_*` directly would be a
    second answer to that question, able to disagree with the first — the same
    shape as the three-names-for-one-question problem the config profiles work
    collapsed (2026-09-04).

    `tools/credcheck.py` is the one exception, and a deliberate one: it is a SHAPE
    diagnostic that must be able to look at every slot at once, including ones the
    current config does not select, and it imports the constants rather than
    spelling them.
    """
    allowed = {_SELECTOR_MODULE, _SRC / "tools" / "credcheck.py"}
    offenders: list[str] = []
    for path in _python_files():
        if path in allowed:
            continue
        text = path.read_text()
        if "TASTYTRADE_" in text:
            for i, line in enumerate(text.splitlines(), start=1):
                if "TASTYTRADE_" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{path.relative_to(_SRC)}:{i}: {line.strip()}")
    assert not offenders, (
        "a credential variable is named outside the selector module:\n  "
        + "\n  ".join(offenders)
        + "\nRoute it through `trading_credentials` / `market_data_credentials`."
    )


def test_the_status_slot_labels_hold_no_value():
    """`credential_slot_labels` must not touch the environment.

    It feeds `/status`, which is served over the control surface. A version that
    read `os.environ` would be one refactor away from rendering a secret, and the
    row's whole claim is that it reports NAMES. Pinned structurally rather than by
    the docstring that says so.
    """
    tree = ast.parse(_SELECTOR_MODULE.read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "credential_slot_labels"
    )
    reads = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Attribute) and n.attr in {"environ", "getenv"}
    ]
    assert not reads, (
        "credential_slot_labels reads the environment; it must answer from config "
        "alone, so it can never hold a value it might render"
    )


@pytest.mark.parametrize(
    "name",
    [
        "TASTYTRADE_CLIENT_SECRET",
        "TASTYTRADE_REFRESH_TOKEN",
        "TASTYTRADE_LIVE_CLIENT_SECRET",
        "TASTYTRADE_LIVE_REFRESH_TOKEN",
    ],
)
def test_the_pre_rename_variable_names_are_gone(name):
    """The old names are REMOVED, not aliased.

    An alias is a second name for one credential, which is the disease — and these
    four were also byte-identical to TOMIC's, isolated only by living in separate
    `.env` files, so any shell that loaded the wrong one won. The additive
    three-step migration is what buys reversibility; a fallback in code would
    quietly keep the ambiguity forever.

    `.env.example` is checked too: it is the file an operator copies, so a stale
    name there re-creates the state on the next machine.

    COMMENTS ARE EXEMPT, deliberately. `broker/session.py` names all four in prose
    to say they are gone and must not come back as aliases, and that sentence is
    the most useful thing a future reader of that block can find. The rule is
    about what the code READS, not about what it is allowed to explain.
    """
    repo = _SRC.parents[1]
    offenders: list[str] = []
    for path in [*_python_files(), repo / ".env.example"]:
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            if name in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.relative_to(repo)}:{i}: {line.strip()}")
    assert not offenders, (
        f"pre-rename credential name {name} still live in:\n  " + "\n  ".join(offenders)
    )
