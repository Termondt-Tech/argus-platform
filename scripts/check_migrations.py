#!/usr/bin/env python3
"""Mechanical enforcement of the migration-immutability convention.

`src/argus/db/migrations.py` states the rule in prose — "Applied files are
immutable — never edit one after it has shipped; add a new file" — and until now
nothing enforced it. The runner records `version` in `schema_migrations` and then
SKIPS that version forever, so editing a shipped migration changes the schema on
every FRESH database while every EXISTING one silently keeps the old shape. There
is no error, no divergence report, and the audit DB that reconciliation reads back
as expected state is exactly what quietly drifts.

Two rules, both checked here:

  1. No migration file already present in the diff base may be Modified, Deleted
     or Renamed. Only additions are legal.
  2. The filenames must form an unbroken sequence 0001..NNNN — no gaps, no
     duplicate numbers. `_discover()` in the runner raises on duplicates at
     import time, which would take down the supervisor at 09:30 rather than in
     review; a gap is subtler and it does not catch that at all.

The core is `check_migrations()`, a pure function over `(git_status, path)` pairs
plus the set of migration filenames, so the rules are unit-tested without a git
repository. `main()` only supplies the two real inputs: `git diff --name-status`
against the resolved base, and a directory listing.

Exit status: 0 clean, 1 violations found, 2 could not run the check.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Repo-relative, forward-slashed — this is compared against git's own output,
# which always uses forward slashes regardless of platform.
MIGRATIONS_DIR = "src/argus/db/migrations"

# Mirrors `_FILENAME_RE` in src/argus/db/migrations.py. Deliberately duplicated
# rather than imported: this script must run in a bare checkout with nothing
# installed, and importing the package would drag in the tastytrade SDK.
FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# git's --name-status codes. Anything not listed is treated as a modification,
# because failing closed on an unrecognised status is the whole point.
_STATUS_LABEL = {
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type-changed",
    "U": "unmerged",
}

_ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class CheckResult:
    """Outcome of the two rules. `ok` is the exit condition; `violations` is prose."""

    ok: bool
    violations: list[str] = field(default_factory=list)


def _is_migration_path(path: str) -> bool:
    return path.startswith(MIGRATIONS_DIR + "/")


def check_migrations(
    changes: list[tuple[str, str]],
    migration_files: set[str],
) -> CheckResult:
    """Apply both rules. Pure — no git, no filesystem, no environment.

    `changes` is `(git_status, repo_relative_path)`; statuses may carry git's
    similarity score (`R100`), which is truncated to its leading letter. A rename
    is expected to appear TWICE, once for each side, so that a migration renamed
    away from its number is caught on the old path even though the new path looks
    like a legal addition.

    `migration_files` is the set of BASENAMES currently in the migrations
    directory — the tree as it would exist after the change lands.
    """
    violations: list[str] = []

    for status, path in changes:
        if not _is_migration_path(path):
            continue
        code = (status or "?")[:1].upper()
        if code == "A":
            continue  # additions are the only legal change
        label = _STATUS_LABEL.get(code, f"changed (git status {status!r})")
        violations.append(
            f"{path} was {label}. Migrations are immutable once they ship: the "
            f"runner records the version and skips it forever, so this changes "
            f"the schema on new databases only. Add a new migration instead."
        )

    violations.extend(_check_sequence(migration_files))
    return CheckResult(ok=not violations, violations=violations)


def _check_sequence(migration_files: set[str]) -> list[str]:
    """Filenames must be NNNN_name.sql forming an unbroken 0001..NNNN run."""
    violations: list[str] = []
    numbers: list[int] = []

    for name in sorted(migration_files):
        m = FILENAME_RE.match(name)
        if not m:
            violations.append(
                f"{MIGRATIONS_DIR}/{name} does not match NNNN_name.sql "
                f"(four digits, then lowercase/underscore). The runner raises on "
                f"this at import time, which would fail the supervisor at startup."
            )
            continue
        numbers.append(int(m.group(1)))

    if not numbers:
        # An empty directory is a broken checkout, not a clean tree.
        violations.append(
            f"no migration files found under {MIGRATIONS_DIR}/ — refusing to "
            f"report a pass, since the check cannot have examined anything."
        )
        return violations

    counts: dict[int, int] = {}
    for n in numbers:
        counts[n] = counts.get(n, 0) + 1
    for n in sorted(k for k, v in counts.items() if v > 1):
        violations.append(
            f"migration number {n:04d} is used by more than one file. The runner "
            f"raises 'duplicate migration numbers' at import time."
        )

    ordered = sorted(set(numbers))
    expected = list(range(1, len(ordered) + 1))
    if ordered != expected:
        missing = sorted(set(expected) - set(ordered))
        if missing:
            gap = ", ".join(f"{n:04d}" for n in missing)
            violations.append(
                f"migration numbering has gaps: {gap} missing. Numbering must run "
                f"unbroken from 0001; a gap usually means a migration was deleted "
                f"or renamed rather than superseded."
            )
        else:
            violations.append(
                f"migration numbering must start at 0001 and increase by one; "
                f"found {', '.join(f'{n:04d}' for n in ordered)}."
            )

    return violations


# ---------------------------------------------------------------------------
# The impure half: resolve a base, ask git, list the directory.
# ---------------------------------------------------------------------------


def parse_name_status(output: str) -> list[tuple[str, str]]:
    """Parse `git diff --name-status` into `(status, path)` pairs.

    Rename and copy records carry two paths (`R100\told\tnew`). BOTH are emitted
    under the original status so that a migration renamed away from its number
    trips rule 1 on the old path — emitting the new path as an addition would let
    exactly that edit through.
    """
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        for path in parts[1:]:
            if path.strip():
                changes.append((status, path.strip()))
    return changes


def resolve_base(env: dict[str, str]) -> str | None:
    """Work out what to diff against, or None to run the sequence check alone.

    - pull_request: the PR base branch, as `origin/<ref>`.
    - push to a non-default branch: `origin/main`.
    - push to main: the previous head (`github.event.before`). On a brand-new
      branch or an initial push that is all zeros and there is nothing to diff,
      so the diff half is skipped rather than guessed at.
    """
    event = env.get("GITHUB_EVENT_NAME", "")

    if event == "pull_request":
        base_ref = env.get("GITHUB_BASE_REF", "").strip()
        return f"origin/{base_ref}" if base_ref else "origin/main"

    if event == "push":
        ref_name = env.get("GITHUB_REF_NAME", "").strip()
        default_branch = env.get("GITHUB_DEFAULT_BRANCH", "main").strip() or "main"
        if ref_name and ref_name != default_branch:
            return f"origin/{default_branch}"
        before = env.get("GITHUB_EVENT_BEFORE", "").strip()
        if not before or set(before) == {"0"} or before == _ZERO_SHA:
            return None
        return before

    # Run locally with no GitHub environment: compare against origin/main, which
    # is what a developer checking their own branch means by "changed".
    return env.get("MIGRATION_CHECK_BASE", "origin/main").strip() or None


def _git_diff(base: str, repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "diff", "--name-status", f"{base}...HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`git diff --name-status {base}...HEAD` failed "
            f"(exit {proc.returncode}): {proc.stderr.strip()}\n"
            f"The workflow needs `fetch-depth: 0` for the base to be present."
        )
    return proc.stdout


def _list_migrations(repo_root: Path) -> set[str]:
    directory = repo_root / MIGRATIONS_DIR
    if not directory.is_dir():
        raise RuntimeError(f"migrations directory not found: {directory}")
    return {p.name for p in directory.iterdir() if p.is_file()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        default=None,
        help="git ref to diff against; overrides GitHub-event resolution.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repository root (default: the parent of scripts/).",
    )
    args = parser.parse_args(argv)

    repo_root = (
        Path(args.repo_root).resolve()
        if args.repo_root
        else Path(__file__).resolve().parent.parent
    )

    try:
        migration_files = _list_migrations(repo_root)
    except RuntimeError as exc:
        print(f"migration-check: cannot run: {exc}", file=sys.stderr)
        return 2

    base = args.base if args.base is not None else resolve_base(dict(os.environ))

    changes: list[tuple[str, str]] = []
    if base:
        try:
            changes = parse_name_status(_git_diff(base, repo_root))
        except RuntimeError as exc:
            print(f"migration-check: cannot run: {exc}", file=sys.stderr)
            return 2
        print(f"migration-check: diffing against {base}", flush=True)
    else:
        print(
            "migration-check: no diff base (new branch or initial push); "
            "running the sequence check against the current tree only.",
            flush=True,
        )

    result = check_migrations(changes, migration_files)

    if result.ok:
        print(
            f"migration-check: OK — {len(migration_files)} migration(s), "
            f"none modified, sequence unbroken.",
            flush=True,
        )
        return 0

    print("\nmigration-check: FAILED\n", file=sys.stderr)
    for violation in result.violations:
        print(f"  - {violation}\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
