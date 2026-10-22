"""`python -m argus` runs every strategy: the registry, and the config it resolves.

The platform contract (docs/decisions/2026-10-22-one-platform-a-strategy-per-process.md):
one entry point; `ARGUS_STRATEGY`, a literal in the strategy's own compose file,
picks the strategy from a literal table; UNSET is the SPX book exactly as before
the registry existed. The derived config name follows the strategy, and nothing
about the SPX book's resolution changes.

Another strategy is a TEST DOUBLE here. Each real strategy pins its own row, and
its own config resolution, in its own test module (`tests/test_<strategy>_registry.py`),
so this file holds for whatever the registry holds: one row, two, or none.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

import argus.__main__ as entry
from argus.config import STRATEGY_CONFIG_FILENAME_RE, ConfigError, resolve_config_path

REPO = Path(__file__).resolve().parents[1]
DOUBLE = "a_strategy"


# -- the config a strategy resolves ---------------------------------------------------


def test_the_spx_books_resolution_is_unchanged():
    env = {"ARGUS_ENV": "live", "ARGUS_CONFIG_DIR": "/etc/argus"}
    assert resolve_config_path(env) == (Path("/etc/argus/argus.live.toml"), "live")


def test_a_strategys_profile_names_its_own_file():
    env = {"ARGUS_ENV": "shadow", "ARGUS_CONFIG_DIR": "/etc/argus"}
    assert resolve_config_path(env, strategy=DOUBLE) == (
        Path(f"/etc/argus/argus.{DOUBLE}.shadow.toml"), "shadow")


def test_an_explicit_path_must_name_the_same_strategy():
    path, profile = resolve_config_path(
        {"ARGUS_CONFIG": f"/x/argus.{DOUBLE}.shadow.toml"}, strategy=DOUBLE)
    assert (path, profile) == (Path(f"/x/argus.{DOUBLE}.shadow.toml"), "shadow")
    with pytest.raises(ConfigError, match=f"is not a '{DOUBLE}' config"):
        resolve_config_path({"ARGUS_CONFIG": "/x/argus.live.toml"}, strategy=DOUBLE)
    with pytest.raises(ConfigError, match="disagrees"):
        resolve_config_path({"ARGUS_CONFIG": f"/x/argus.{DOUBLE}.shadow.toml",
                             "ARGUS_ENV": "dev"}, strategy=DOUBLE)


def test_no_profile_is_still_a_refusal_for_every_strategy():
    for strategy in (None, DOUBLE):
        with pytest.raises(ConfigError, match="no config selected"):
            resolve_config_path({}, strategy=strategy)


# -- the registry ---------------------------------------------------------------------


def _dispatcher_imports(run) -> list[str]:
    """The modules a registry entry imports INSIDE its body: its strategy's package."""
    tree = ast.parse(inspect.getsource(run))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    names: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def test_the_registry_is_a_literal_table_of_dispatchers():
    """Every row maps a usable strategy name to a function defined in the entry point
    itself, whose import of the strategy's package sits in its body: the table can be
    read, and the SPX book started, without importing any strategy."""
    assert isinstance(entry.STRATEGIES, dict)
    for name, run in entry.STRATEGIES.items():
        assert STRATEGY_CONFIG_FILENAME_RE.match(f"argus.{name}.dev.toml"), name
        assert callable(run) and run.__module__ == entry.__name__, name
        assert _dispatcher_imports(run), f"{name}: its dispatcher imports nothing"


@pytest.fixture
def quiet_env(monkeypatch):
    """Never read the repo's real .env into the test process."""
    monkeypatch.setattr(entry, "load_env", lambda: False)
    monkeypatch.delenv("ARGUS_STRATEGY", raising=False)
    return monkeypatch


def test_a_named_strategy_runs_its_entry_point(quiet_env):
    ran: list[str] = []
    quiet_env.setitem(entry.STRATEGIES, DOUBLE, lambda: ran.append(DOUBLE))
    quiet_env.setenv("ARGUS_STRATEGY", DOUBLE)
    entry.main()
    assert ran == [DOUBLE]


def test_an_unknown_strategy_is_refused(quiet_env):
    quiet_env.setenv("ARGUS_STRATEGY", "spx_condor")
    with pytest.raises(ConfigError, match="not a strategy this build runs"):
        entry.main()


def test_unset_is_the_spx_book_exactly_as_before(quiet_env):
    class Reached(Exception):
        pass

    def load():
        raise Reached
    quiet_env.setattr(entry.Config, "load", staticmethod(load))
    with pytest.raises(Reached):
        entry.main()


def test_the_spx_process_imports_no_other_strategys_package():
    """Importing the entry point (what the SPX process does) loads none of the packages
    the registry's rows import: each import sits inside its dispatcher. The package is
    derived from the dispatcher's own import, so a new row is covered by construction."""
    packages = sorted({".".join(m.split(".")[:2])
                       for run in entry.STRATEGIES.values()
                       for m in _dispatcher_imports(run)})
    assert "argus" not in packages, packages          # a strategy is a package UNDER argus
    code = ("import sys, argus.__main__\n"
            f"packages = {packages!r}\n"
            "bad = sorted(m for m in sys.modules\n"
            "             if any(m == p or m.startswith(p + '.') for p in packages))\n"
            "assert not bad, bad\n")
    subprocess.run([sys.executable, "-c", code], check=True, cwd=REPO, timeout=60)
