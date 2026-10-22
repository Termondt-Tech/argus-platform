"""What build is actually running — `/status`, the startup log, and the audit trail.

The gap this closes is not hypothetical. During the chunk 1-4 cutover, `main` did not
contain the chunks and a build off it would have deployed a supervisor missing the
entire execution side; a pre-flight check caught it by luck, because nothing at
runtime could be asked. `git describe --tags --always --dirty` is computed at IMAGE
BUILD TIME and baked into `argus/_version.py` (the runtime image carries no `.git`
and no git binary, deliberately), so the answer is one field on a page reachable from
a phone.

The tests never require a git-tagged checkout: the version is injected the way every
other piece of configuration is. What they DO pin is the coupling that cannot be
tested any other way — that `Dockerfile`'s stamp still matches the line it rewrites.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from argus._version import UNKNOWN, build_version, is_dirty
from argus.audit.log import AuditLog
from argus.db import apply_migrations, connect

from tests.support import make_config
from tests.test_status import _make_service

_REPO = pathlib.Path(__file__).resolve().parents[1]


async def _snapshot(db, **service_kwargs):
    audit = AuditLog(db, tier2_queue_maxsize=10, tier2_queue_alert_depth=8)
    audit.start()
    svc, _ss, _ks = _make_service(audit, **service_kwargs)
    try:
        snap = await svc.assemble()
        return snap, snap.to_json_dict(), snap.to_html()
    finally:
        await audit.aclose()


# -- the module ---------------------------------------------------------------


def test_an_unstamped_build_reports_unknown_rather_than_blank():
    """The committed value, which is what a dev checkout and any build that omitted
    the arg both report. A word, not an empty string: a blank field on the page reads
    as a broken page rather than as an unstamped build, and this is not a fault."""
    assert build_version() == UNKNOWN


def test_a_blank_stamp_normalizes_rather_than_rendering_empty(monkeypatch):
    """`--build-arg ARGUS_VERSION=` writes an empty literal. Same reasoning: the page
    must never show nothing where it promised a version."""
    monkeypatch.setattr("argus._version.BUILD_VERSION", "   ")
    assert build_version() == UNKNOWN


@pytest.mark.parametrize(
    ("version", "dirty"),
    [
        ("v0.3.14", False),
        ("v0.3.14-2-gdeadbee", False),
        ("v0.3.14-2-gdeadbee-dirty", True),
        ("deadbee-dirty", True),
        (UNKNOWN, False),
    ],
)
def test_dirty_is_read_off_the_suffix_git_actually_appends(version, dirty):
    assert is_dirty(version) is dirty


# -- /status ------------------------------------------------------------------


async def test_status_surfaces_whatever_version_the_build_stamped(db):
    """Both renderings, from the one assembly — the JSON for a script and the HTML for
    the phone. `-dirty` is absent here, so nothing qualifies it."""
    _snap, js, html_out = await _snapshot(db, version="v0.3.14")
    assert js["version"] == "v0.3.14"
    assert "v0.3.14" in html_out
    assert "Build" in html_out
    assert "uncommitted changes" not in html_out


async def test_an_unstamped_build_says_so_on_the_page_instead_of_going_quiet(db):
    """The fallback has to READ as a fallback. A muted `unknown` with the reason
    beside it is the difference between "nobody stamped this" and "the field is
    broken" — and the fix belongs in the deploy, so the row points at it."""
    _snap, js, html_out = await _snapshot(db)
    assert js["version"] == UNKNOWN
    assert "unknown" in html_out
    assert "ARGUS_VERSION" in html_out


async def test_a_dirty_build_is_flagged_in_words_not_only_in_colour(db):
    """Exactly the "what is running is not what I expect" signal the field exists for.

    Amber, not red, and that is a judgment about false positives rather than about
    severity: the droplet's `argus.toml` is tracked AND edited in place, so a dirty
    build tree is an ordinary condition there. Red on a usually-benign condition is
    how a red badge stops meaning anything — the rule `_link_cell` already states for
    `reconnects`."""
    _snap, js, html_out = await _snapshot(db, version="v0.3.14-2-gdeadbee-dirty")
    assert js["version"] == "v0.3.14-2-gdeadbee-dirty"
    build_cell = html_out[html_out.index("Build"):]
    assert "uncommitted changes" in build_cell
    assert "class='warn'" in build_cell
    assert "class='bad'" not in build_cell.split("Phase")[0]


async def test_the_version_does_not_disturb_any_existing_field(db):
    """Purely additive: the snapshot is one object rendered two ways, so a new field
    is exactly one new key and one new row."""
    _snap, js, _html = await _snapshot(db, version="v0.3.14")
    assert set(js) >= {"environment", "process", "session", "kill_switch", "halted"}
    assert js["environment"] == "shadow"
    # Serializable, which the route depends on.
    json.dumps(js)


# -- the process ---------------------------------------------------------------


async def test_startup_logs_the_version_and_records_it_in_the_chain(
    tmp_path: pathlib.Path, caplog
):
    """Two durable answers to "which code wrote this?", for two different readers.

    The log line is FIRST in `startup()` — before anything that can refuse — because
    a failed startup is when the question is most urgent, and a version that only
    printed on success would be missing exactly then. The audit row outlives the
    journal, which rotates.
    """
    from argus.app import Application

    cfg = make_config(audit={"db_path": str(tmp_path / "argus.db")})
    app = Application(cfg)
    with caplog.at_level("INFO", logger="argus.app"):
        await app.startup()
    try:
        assert any(
            "argus build" in r.getMessage() for r in caplog.records
        ), "the build must be logged at startup"
        assert app.status_service is not None
        snap = await app.status_service.assemble()
        assert snap.version == build_version()
    finally:
        await app.shutdown()

    conn = connect(tmp_path / "argus.db")
    apply_migrations(conn)
    (payload,) = [
        json.loads(row["payload"])
        for row in conn.execute(
            "SELECT payload FROM audit_log WHERE category = 'process.start'"
        )
    ]
    conn.close()
    assert payload["version"] == build_version()


# -- the coupling nothing else can see ----------------------------------------


def test_the_dockerfile_still_stamps_the_line_the_module_declares():
    """`Dockerfile` rewrites `_version.py` by matching the literal prefix
    `BUILD_VERSION = ` at the start of a line, and this is the only place that
    coupling is visible. Reformat the assignment (a type annotation, a rename, a move
    into a class) and every release image ships a confident `unknown` — the exact
    "stale but not crashing version label" this feature exists to prevent.

    Not a substitute for the `grep -q` assertion inside the build, which catches the
    same break at build time. This catches it in CI, before an image is ever cut.
    """
    module = (_REPO / "src" / "argus" / "_version.py").read_text()
    assert re.search(r'^BUILD_VERSION = "unknown"$', module, re.MULTILINE), (
        "the committed fallback must be a single line matching the Dockerfile's sed"
    )

    dockerfile = (_REPO / "Dockerfile").read_text()
    assert "ARG ARGUS_VERSION" in dockerfile, "the build arg must be declared"
    assert "^BUILD_VERSION = .*" in dockerfile, (
        "the Dockerfile's sed pattern must still match the module's assignment"
    )
    assert "src/argus/_version.py" in dockerfile
    # Build-time, not runtime: no BUILD STEP may reach for git inside the image.
    # Comments are stripped first — the reasoning names the command the CALLER runs,
    # and the whole point of writing it down is that it happens outside the build.
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    assert "git" not in instructions, (
        "the version is an INPUT to the build; the image carries no .git and no git"
    )


def test_no_script_builds_the_image_without_stamping_it():
    """The runbook and `scripts/argus-deploy.sh` are two implementations of ONE
    procedure, and updating only the prose is how this feature nearly shipped inert.

    A doc that says the wrong thing gets read by a human who can notice. A SCRIPT that
    does the wrong thing produces a clean build, a clean restart, a green journal —
    and `version: "unknown"` forever. Found by review on 2026-09-03, after the runbook
    had already been updated to a command the script it names does not run.

    So the invariant is not "the runbook is right", it is: **no executable path builds
    the image without passing `ARGUS_VERSION`.** Line continuations are joined first,
    because the real invocation spans two lines and a naive line scan would miss it —
    which is exactly the kind of near-miss this test is for.
    """
    scripts = sorted((_REPO / "scripts").glob("*.sh"))
    assert scripts, "no deploy scripts found — has the directory moved?"

    checked = 0
    for path in scripts:
        # Join `\`-continued lines, then drop comments: the reasoning above the build
        # mentions the flag in prose, and prose is not an invocation.
        joined = path.read_text().replace("\\\n", " ")
        for line in joined.splitlines():
            code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
            # `docker build` (the deploy, since v0.6.0: the platform's one image) or
            # `docker compose ... build` (any script that builds through compose).
            builds = "docker build" in code or (
                "docker compose" in code and re.search(r"\bbuild\b", code))
            if not builds:
                continue
            checked += 1
            assert "ARGUS_VERSION" in code, (
                f"{path.name} builds the image without stamping it:\n  {code.strip()}\n"
                "A build with no --build-arg succeeds and reports version 'unknown'."
            )
    assert checked, (
        "no image build (`docker build`, `docker compose ... build`) found in scripts/ — either the "
        "deploy script stopped building the image, or this test stopped matching it"
    )


def test_the_deploy_script_reads_the_running_version_back():
    """The other half, and the only check that can catch it: `argus.service` has no
    build step, so a `systemctl restart` with no fresh build silently reattaches to
    the previous image — version label included. Nothing errors. The runbook can warn
    about the ordering; only the deploy can verify the outcome.

    Pinned structurally because it is a whole-procedure property that no unit test of
    any module can express.
    """
    script = (_REPO / "scripts" / "argus-deploy.sh").read_text()
    assert "argus build" in script, (
        "the deploy must grep the startup line for the running build"
    )
    assert "RUNNING_VERSION" in script and "$ACTUAL" in script, (
        "the deploy must compare what came up against what it deployed"
    )
