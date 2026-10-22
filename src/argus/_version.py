"""The running build's git version — stamped at IMAGE BUILD TIME, never computed here.

`/status` and the startup log answer "what is actually deployed?" from this one
string. It exists because the answer used to require git archaeology on the droplet,
and once it was wrong in the direction that matters: during the chunk 1-4 cutover,
`main` did not contain the chunks, and nothing at runtime said so. A pre-flight check
caught it by luck.

**Build-time, deliberately.** The runtime image contains no `.git` and no git binary
(and must not — see the deployment runbook), so nothing in a running container can
derive this. `Dockerfile` rewrites the `BUILD_VERSION` line below from the
`ARGUS_VERSION` build arg, which the documented build command computes with
`git describe --tags --always --dirty`. An env var set at `docker compose up` time
was rejected for the reason that makes this feature worth having at all: it is one
more thing to forget, and forgetting it produces a confident wrong answer rather than
a missing one.

**The committed value is the fallback, and it is honest.** An ad hoc local build (or
a plain `./.venv/bin/python -m argus` from the repo) reports `unknown`, which says
exactly what is true — nobody stamped this build — rather than a blank field or a
misleading number. `unknown` is never an error and never refuses startup: this is a
monitoring label, not a rail.

**Do not reformat the assignment below.** `Dockerfile` matches it on the literal
prefix `BUILD_VERSION = ` at the start of a line, and asserts the substitution
landed, so a build that silently failed to stamp fails loudly instead.
"""

from __future__ import annotations

#: What an unstamped build reports. A word, not an empty string — every renderer here
#: distinguishes "absent" from "present and empty", and this is neither: it is a known
#: state with a name.
UNKNOWN = "unknown"

BUILD_VERSION = "unknown"


def build_version() -> str:
    """The stamped version, or `unknown`.

    Normalizes rather than trusting the constant: a build arg passed as an empty
    string writes an empty literal, and a blank field on the page reads as a bug in
    the page rather than as an unstamped build.
    """
    return BUILD_VERSION.strip() or UNKNOWN


def is_dirty(version: str) -> bool:
    """Whether the tree carried uncommitted changes to TRACKED files when built.

    `git describe --dirty` appends the suffix. Worth surfacing — it is the same
    "what is running is not what I think" signal this module exists for — but
    deliberately not styled as an emergency on the page: the droplet's config file
    is tracked AND edited in place (`[server] bind_host` is that box's Tailscale IP),
    so a dirty tree at build time is an ordinary condition there, not a fault. See
    `status._version_cell`.
    """
    return version.endswith("-dirty")
