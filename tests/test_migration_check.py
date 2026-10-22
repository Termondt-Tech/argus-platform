"""The migration-immutability check (scripts/check_migrations.py).

The rule it enforces used to live only in a docstring: migrations are numbered
and never edited once shipped. Editing one is silent — the runner records the
version and skips it forever, so a fresh database gets the new schema and every
existing one keeps the old. These tests cover the pure decision function; the
CI entrypoint only feeds it `git diff --name-status` output and a directory
listing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_migrations.py"


def _load():
    """Load the script by path — `scripts/` is not a package and must not become
    one; the check has to run in a bare checkout with nothing installed."""
    spec = importlib.util.spec_from_file_location("check_migrations", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the script uses `from __future__ import annotations`,
    # and @dataclass resolves those string annotations through sys.modules.
    sys.modules["check_migrations"] = module
    spec.loader.exec_module(module)
    return module


cm = _load()
MIG = cm.MIGRATIONS_DIR

# The sequence as it actually stands in the repo today.
CLEAN_FILES = {
    "0001_initial.sql",
    "0002_positions.sql",
    "0003_audit_status_index.sql",
    "0004_orders.sql",
    "0005_order_leg_fill_prices.sql",
}


def _path(name: str) -> str:
    return f"{MIG}/{name}"


# -- rule 1: nothing already shipped may change ------------------------------


def test_empty_diff_passes():
    result = cm.check_migrations([], CLEAN_FILES)
    assert result.ok
    assert result.violations == []


def test_clean_addition_passes():
    files = CLEAN_FILES | {"0006_fills.sql"}
    result = cm.check_migrations([("A", _path("0006_fills.sql"))], files)
    assert result.ok, result.violations


def test_modified_migration_fails_and_names_the_file():
    result = cm.check_migrations([("M", _path("0002_positions.sql"))], CLEAN_FILES)
    assert not result.ok
    assert any("0002_positions.sql" in v and "modified" in v for v in result.violations)


def test_deleted_migration_fails():
    files = CLEAN_FILES - {"0004_orders.sql"}
    result = cm.check_migrations([("D", _path("0004_orders.sql"))], files)
    assert not result.ok
    assert any("0004_orders.sql" in v and "deleted" in v for v in result.violations)


def test_renamed_migration_fails_on_the_old_path():
    """A rename emits both sides. The new path looks like a legal addition, so the
    old path is what has to trip the rule — otherwise renaming 0002 to 0005 and
    editing it would pass."""
    files = (CLEAN_FILES - {"0002_positions.sql"}) | {"0002_renamed.sql"}
    changes = [
        ("R100", _path("0002_positions.sql")),
        ("R100", _path("0002_renamed.sql")),
    ]
    result = cm.check_migrations(changes, files)
    assert not result.ok
    assert any("0002_positions.sql" in v and "renamed" in v for v in result.violations)


def test_changes_outside_the_migrations_directory_are_ignored():
    changes = [
        ("M", "src/argus/db/migrations.py"),  # the runner itself, not a migration
        ("M", "src/argus/session.py"),
        ("D", "README.md"),
    ]
    result = cm.check_migrations(changes, CLEAN_FILES)
    assert result.ok, result.violations


def test_unknown_status_fails_closed():
    """An unrecognised git status must not be read as 'fine'."""
    result = cm.check_migrations([("X", _path("0001_initial.sql"))], CLEAN_FILES)
    assert not result.ok
    assert any("0001_initial.sql" in v for v in result.violations)


# -- rule 2: the sequence -----------------------------------------------------


def test_gap_in_sequence_fails():
    files = {"0001_initial.sql", "0002_positions.sql", "0004_orders.sql"}
    result = cm.check_migrations([], files)
    assert not result.ok
    assert any("gap" in v.lower() and "0003" in v for v in result.violations)


def test_duplicate_number_fails():
    files = CLEAN_FILES | {"0004_orders_again.sql"}
    result = cm.check_migrations([("A", _path("0004_orders_again.sql"))], files)
    assert not result.ok
    assert any("0004" in v and "more than one" in v for v in result.violations)


def test_sequence_not_starting_at_one_fails():
    files = {"0002_positions.sql", "0003_audit_status_index.sql"}
    result = cm.check_migrations([], files)
    assert not result.ok
    assert any("0001" in v for v in result.violations)


def test_malformed_filename_fails():
    files = CLEAN_FILES | {"005_missing_a_digit.sql"}
    result = cm.check_migrations([], files)
    assert not result.ok
    assert any("NNNN_name.sql" in v for v in result.violations)


def test_empty_migrations_directory_fails_rather_than_passing_vacuously():
    result = cm.check_migrations([], set())
    assert not result.ok
    assert any("no migration files" in v for v in result.violations)


def test_addition_out_of_sequence_fails_even_though_it_is_an_addition():
    """Rule 1 permits additions; rule 2 still constrains where they land."""
    files = CLEAN_FILES | {"0009_far_future.sql"}
    result = cm.check_migrations([("A", _path("0009_far_future.sql"))], files)
    assert not result.ok
    assert any("gap" in v.lower() for v in result.violations)


# -- the real tree ------------------------------------------------------------


def test_the_repository_as_it_stands_is_clean():
    """Guards against the check being wrong about this repo's own layout."""
    repo_root = Path(__file__).resolve().parent.parent
    files = {p.name for p in (repo_root / MIG).iterdir() if p.is_file()}
    result = cm.check_migrations([], files)
    assert result.ok, result.violations


# -- parsing git's output -----------------------------------------------------


def test_parse_name_status_handles_renames_and_plain_entries():
    output = "M\tsrc/a.py\nA\tsrc/b.py\nR100\told/c.sql\tnew/c.sql\n"
    assert cm.parse_name_status(output) == [
        ("M", "src/a.py"),
        ("A", "src/b.py"),
        ("R100", "old/c.sql"),
        ("R100", "new/c.sql"),
    ]


def test_parse_name_status_ignores_blank_lines():
    assert cm.parse_name_status("\n\n") == []


# -- diff-base resolution -----------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        # A PR diffs against the branch it targets.
        (
            {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_BASE_REF": "main"},
            "origin/main",
        ),
        # A push to a feature branch diffs against the default branch.
        (
            {
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_NAME": "feature-x",
                "GITHUB_EVENT_BEFORE": "abc123",
            },
            "origin/main",
        ),
        # A push to main diffs against the previous head.
        (
            {
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_NAME": "main",
                "GITHUB_EVENT_BEFORE": "abc123",
            },
            "abc123",
        ),
    ],
)
def test_resolve_base(env, expected):
    assert cm.resolve_base(env) == expected


def test_resolve_base_returns_none_on_the_all_zeros_sha():
    """A new branch or initial push has no previous head; the sequence check
    still runs, but there is nothing to diff and nothing may be invented."""
    env = {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF_NAME": "main",
        "GITHUB_EVENT_BEFORE": "0" * 40,
    }
    assert cm.resolve_base(env) is None
