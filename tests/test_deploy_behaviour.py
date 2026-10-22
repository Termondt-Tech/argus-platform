"""`scripts/argus-deploy.sh`, RUN: it restarts what is running and never starts a unit.

`test_deploy_wiring.py` pins the script's shape. This runs it, end to end, against a
throwaway repository and tag pair, with `sudo`, `docker`, `systemctl` and
`journalctl` replaced by recording stand-ins on PATH, because the property that
matters most is behavioural: **on the droplet the SPX book's unit is stopped and
disabled (its kill switch engaged since 2026-10-01), and a deploy must leave it so**,
while a RUNNING unit is restarted once and its startup line read back
(docs/decisions/2026-10-22-one-platform-a-strategy-per-process.md).

THE PLATFORM'S MODULE. The throwaway host is built from the deploy script's own
STRATEGY UNITS table (`test_deploy_wiring.deploy_rows`): each row's unit file, its
mounts and a config file per profile, and each row's per-host profile file handed
to the script through the env override its row names. So `Host` stands with one
row on the table or several, and every case here drives the SPX book's own unit,
`argus.service`, under its live profile. Another strategy's cases are its own
module's, on this same `Host`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from test_deploy_wiring import DeployRow, deploy_rows

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "argus-deploy.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None or os.geteuid() == 0,
    reason="needs bash and git, and the script refuses to run as root")

SHIMS = {
    # sudo VAR=val cmd args...  ->  env VAR=val cmd args...  (no privilege here)
    "sudo": '#!/usr/bin/env bash\nexec env "$@"\n',
    # `docker build -t a -t b` records its tags; `docker image inspect <ref>` answers
    # from them, so "is there an image for this tag?" is as true as the host's.
    "docker": (
        '#!/usr/bin/env bash\n'
        'echo "docker $*" >> "$STATE/calls"\n'
        'case "$1" in\n'
        '  build) prev=""; for a in "$@"; do [ "$prev" = -t ] && echo "$a" >> "$STATE/images";'
        ' prev="$a"; done ;;\n'
        '  image) [ "$2" = inspect ] && exec grep -qx "$3" "$STATE/images" ;;\n'
        '  images) echo "argus  v0.0.2" ;;\n'
        'esac\n'),
    "systemctl": (
        '#!/usr/bin/env bash\n'
        'echo "systemctl $*" >> "$STATE/calls"\n'
        'case "$1" in\n'
        '  is-active) grep -qx "$3" "$STATE/active" ;;\n'
        '  try-restart|daemon-reload) exit 0 ;;\n'
        '  *) echo "VIOLATION systemctl $*" >> "$STATE/calls"; exit 0 ;;\n'
        'esac\n'),
    # journal-<unit> is THIS boot's lines (all a `--since` query sees); journal-old-<unit>
    # is what an earlier boot left, which every other query also returns.
    "journalctl": (
        '#!/usr/bin/env bash\n'
        'echo "journalctl $*" >> "$STATE/calls"\n'
        'unit="$2"\n'
        'case " $* " in *" --since "*) ;; *) [ -f "$STATE/journal-old-$unit" ] &&'
        ' cat "$STATE/journal-old-$unit" ;; esac\n'
        '[ -f "$STATE/journal-$unit" ] && cat "$STATE/journal-$unit"\n'
        'exit 0\n'),
}

#: Each row's config file exists for both of these profiles in the throwaway
#: repository. The `live` one is unchanged between the two tags and the `shadow`
#: one moves (`# 2` appended at v0.0.2), so a dry run has both shapes to show. To
#: the script a profile is a name and a config file is bytes to fingerprint.
PROFILES = ("live", "shadow")
MOVED_PROFILE = "shadow"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "init.defaultBranch=main", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _released_script(tag: str) -> str | None:
    """An earlier release's deploy script, from this repository's own tags (absent in
    a shallow clone without tags, where the test that needs it is skipped)."""
    try:
        return subprocess.run(["git", "show", f"{tag}:scripts/argus-deploy.sh"], cwd=REPO,
                              check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


class Host:
    """A droplet in a directory: the checkout, the units, the profiles, the shims.

    Built from THE STRATEGY UNITS table: every row's unit file and mounts are copied
    from this repository, its config file written for each of `PROFILES`, and its
    per-host profile file placed under `etc/` and handed to the script through the
    env override the row names (`ARGUS_HOST_ENV` for the SPX book's row)."""

    def __init__(self, root: Path, *, first_script: str | None = None,
                 start: str = "v0.0.1") -> None:
        self.rows = deploy_rows()
        self._first_script = first_script
        self._start = start
        self.root = root
        self.work = root / "work"
        self.state = root / "state"
        self.systemd = root / "systemd"
        self.etc = root / "etc"
        for d in (self.state, self.systemd, self.etc, root / "bin"):
            d.mkdir(parents=True)
        (self.state / "calls").write_text("")
        (self.state / "active").write_text("")
        (self.state / "images").write_text("")
        for name, body in SHIMS.items():
            p = root / "bin" / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self._repo()

    def row(self, unit: str) -> DeployRow:
        (row,) = [r for r in self.rows if r.unit == unit]
        return row

    def env_path(self, row: DeployRow) -> Path:
        """Where this host keeps the row's per-host profile file."""
        return self.etc / Path(row.env_file).name

    def _repo(self) -> None:
        remote = self.root / "remote.git"
        _git(self.root, "init", "--bare", str(remote))
        _git(self.root, "init", str(self.work))
        w = self.work
        (w / "scripts").mkdir()
        if self._first_script is not None:      # v0.0.1 ships an older deploy script
            (w / "scripts" / "argus-deploy.sh").write_text(self._first_script)
        else:
            shutil.copy(SCRIPT, w / "scripts" / "argus-deploy.sh")
        shutil.copy(REPO / "Dockerfile", w / "Dockerfile")
        for row in self.rows:
            for name in (row.unit_file, *row.mounts):
                (w / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO / name, w / name)
            for profile in PROFILES:
                (w / row.config(profile)).write_text(f'environment = "{profile}"\n')
        (w / "src").mkdir()
        (w / "src" / "code.py").write_text("v = 1\n")
        _git(w, "add", "-A")
        _git(w, "commit", "-m", "one")
        _git(w, "tag", "v0.0.1")
        (w / "src" / "code.py").write_text("v = 2\n")             # a build input moves
        shutil.copy(SCRIPT, w / "scripts" / "argus-deploy.sh")    # v0.0.2 ships this one
        for row in self.rows:                                     # ...and one profile's config
            moved = w / row.config(MOVED_PROFILE)
            moved.write_text(moved.read_text() + "# 2\n")
        _git(w, "commit", "-am", "two")
        _git(w, "tag", "v0.0.2")
        _git(w, "remote", "add", "origin", str(remote))
        _git(w, "push", "-q", "origin", "main", "--tags")
        _git(w, "fetch", "-q", "origin")
        _git(w, "checkout", "-q", self._start)

    def install(self, unit: str, profile: str, *, running: bool) -> None:
        """Install a row's unit on this host under `profile`, running or stopped."""
        row = self.row(unit)
        shutil.copy(self.work / row.unit_file, self.systemd / unit)
        self.env_path(row).write_text(f"ARGUS_ENV={profile}\n")
        if running:
            with (self.state / "active").open("a") as f:
                f.write(unit + "\n")

    def journal(self, unit: str, line: str) -> None:
        (self.state / f"journal-{unit}").write_text(line + "\n")

    def config_sha(self, filename: str, tag: str = "v0.0.2") -> str:
        """The fingerprint the deploy expects a process on `tag` to report for
        `filename`: the first 12 hex of the sha256 of the tag's bytes."""
        shown = subprocess.run(["git", "show", f"{tag}:{filename}"], cwd=self.work,
                               check=True, capture_output=True)
        return _sha12(shown.stdout)

    def deploy(self, tag: str = "v0.0.2", *flags: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ,
               "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
               "STATE": str(self.state),
               "ARGUS_REPO_DIR": str(self.work),
               **{row.env_var: str(self.env_path(row)) for row in self.rows},
               "ARGUS_SYSTEMD_DIR": str(self.systemd),
               "ARGUS_DEPLOY_SETTLE_S": "0"}
        return subprocess.run(["bash", str(self.work / "scripts" / "argus-deploy.sh"), *flags, tag],
                              env=env, capture_output=True, text=True, timeout=120)

    def calls(self) -> list[str]:
        return (self.state / "calls").read_text().splitlines()


@pytest.fixture
def host(tmp_path) -> Host:
    return Host(tmp_path)


def _spx_line(host: Host, version: str = "v0.0.2") -> str:
    """What `argus.app` logs when the SPX book comes up under `live`: the startup
    line the deploy reads back (`argus build <version> config <profile>/<file>
    @<sha>`), then the good sign the read-back looks for once a session is up."""
    sha = host.config_sha("argus.live.toml", version)
    return (f"INFO argus.app argus build {version} config live/argus.live.toml @{sha}\n"
            f"INFO argus.app argus started in live mode")


def _unit_calls(host: Host, unit: str) -> list[str]:
    return [c for c in host.calls() if unit in c]


# -- restart what is running, read it back; never start what is not ------------------


def test_a_deploy_restarts_the_running_spx_book_once_and_reads_it_back(host):
    """The ordinary deploy of a running unit: one image built and stamped with the
    tag, the unit `try-restart`ed once, and its own journal read back for the
    build it reports and the config bytes it loaded — both compared, not printed."""
    host.install("argus.service", "live", running=True)
    host.journal("argus.service", _spx_line(host))
    done = host.deploy()
    assert done.returncode == 0, done.stdout + done.stderr
    calls = host.calls()
    assert not [c for c in calls if c.startswith("VIOLATION")], calls
    # The image: built once, stamped with the tag.
    builds = [c for c in calls if c.startswith("docker build")]
    assert len(builds) == 1 and builds[0].startswith("docker build --build-arg ARGUS_VERSION=v0.0.2")
    assert calls.count("systemctl try-restart argus.service") == 1
    assert "running build reports: v0.0.2" in done.stdout
    sha = host.config_sha("argus.live.toml")
    assert f"running config reports: live/argus.live.toml @{sha}" in done.stdout
    assert "journal looks clean" in done.stdout
    # No other row was touched: not installed here, so never named in a call.
    for row in host.rows[1:]:
        assert not _unit_calls(host, row.unit), row.unit


def test_a_deploy_leaves_the_stopped_spx_book_stopped(host):
    """THE DROPLET'S STATE: the SPX book's unit installed and stopped. The deploy
    asks whether it is running and does nothing else to it — no `try-restart`, no
    read-back — while the checkout and the image are still put in place for
    whenever a person starts it on purpose."""
    host.install("argus.service", "live", running=False)
    done = host.deploy()
    assert done.returncode == 0, done.stdout + done.stderr
    calls = host.calls()
    assert not [c for c in calls if c.startswith("VIOLATION")], calls
    assert _unit_calls(host, "argus.service") == ["systemctl is-active --quiet argus.service"]
    assert not [c for c in calls if "try-restart" in c]
    assert any(c.startswith("docker build --build-arg ARGUS_VERSION=v0.0.2") for c in calls)
    assert "argus.service is NOT RUNNING" in done.stdout
    assert "argus.service: not running — nothing to read back" in done.stdout


def test_a_unit_not_installed_is_skipped_and_never_touched(host):
    """Installing a strategy's unit is a one-time step by hand, never a deploy's: a
    row whose unit is not on this host is reported as skipped and its name reaches
    no docker, systemctl or journalctl call at all."""
    done = host.deploy()                       # nothing installed on this host
    assert done.returncode == 0, done.stdout + done.stderr
    for row in host.rows:
        assert f"{row.unit}: not installed on this host" in done.stdout
        assert not _unit_calls(host, row.unit), row.unit
    assert not [c for c in host.calls() if c.startswith(("systemctl", "journalctl"))]


def test_a_running_unit_that_came_up_on_the_wrong_config_fails_the_deploy(host):
    host.install("argus.service", "live", running=True)
    host.journal("argus.service",
                 "argus build v0.0.2 config live/argus.live.toml @000000000000")
    done = host.deploy()
    assert done.returncode == 2
    assert "RUNNING CONFIG @000000000000" in done.stderr


def test_an_installed_unit_without_its_profile_file_stops_the_deploy(host):
    shutil.copy(host.work / "argus.service", host.systemd / "argus.service")
    done = host.deploy()
    assert done.returncode == 1
    assert "cannot read" in done.stderr
    assert not [c for c in host.calls() if c.startswith(("docker build", "systemctl try"))]


# -- the hand-over to the script the tag ships --------------------------------------


def test_the_first_deploy_hands_over_before_the_old_script_can_start_the_stopped_spx_book(tmp_path):
    """THE REAL FIRST DEPLOY: the droplet runs v0.5.4's script, which ends in
    `systemctl restart argus.service` (it would start the killed SPX book). It hands
    over to the release's own script after the checkout and before any build or
    restart, so the new rules govern from the first deploy on."""
    old = _released_script("v0.5.4")
    if old is None:
        pytest.skip("v0.5.4 is not in this clone's tags")
    host = Host(tmp_path, first_script=old)
    host.install("argus.service", "live", running=False)
    done = host.deploy()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "handing over to it" in done.stdout
    calls = host.calls()
    assert not [c for c in calls if c.startswith("VIOLATION")], calls
    assert not [c for c in calls if "try-restart" in c]
    assert "argus.service is NOT RUNNING" in done.stdout


def test_the_first_deploy_hands_over_before_the_old_script_can_restart_a_running_unit(tmp_path):
    """The same hand-over with the unit RUNNING: the old script's `systemctl
    restart` never runs (it would be a VIOLATION in the stand-in), the release's own
    script `try-restart`s the unit instead, and reads it back."""
    old = _released_script("v0.5.4")
    if old is None:
        pytest.skip("v0.5.4 is not in this clone's tags")
    host = Host(tmp_path, first_script=old)
    host.install("argus.service", "live", running=True)
    host.journal("argus.service", _spx_line(host))
    done = host.deploy()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "handing over to it" in done.stdout
    calls = host.calls()
    assert not [c for c in calls if c.startswith("VIOLATION")], calls
    assert calls.count("systemctl try-restart argus.service") == 1
    assert "running config reports: live/argus.live.toml" in done.stdout


def test_a_rollback_to_a_script_that_would_start_the_spx_book_is_refused(tmp_path):
    """The other direction: rolling back from v0.6.0 to an older tag hands the build
    and restart to THAT tag's script, which ends in `systemctl restart argus.service`.
    With the SPX book stopped, the deploy refuses before it checks anything out."""
    old = _released_script("v0.5.4")
    if old is None:
        pytest.skip("v0.5.4 is not in this clone's tags")
    host = Host(tmp_path, first_script=old, start="v0.0.2")
    host.install("argus.service", "live", running=False)
    done = host.deploy("v0.0.1")
    assert done.returncode == 1
    assert "would START that stopped unit" in done.stderr
    assert _git(host.work, "describe", "--tags") == "v0.0.2"      # nothing checked out
    assert not [c for c in host.calls() if c.startswith(("docker", "VIOLATION"))]
    assert not [c for c in host.calls() if "restart" in c]


def test_the_hand_over_survives_a_tag_pushed_before_its_branch(tmp_path):
    """v0.6.0's own review: with only the tag pushed, the release's commit is not on
    origin/main. The first run checks for hand-made commits BEFORE its checkout; the
    handed-over run must not ask again with HEAD on the tag, or the deploy stops half
    done (checked out, not built)."""
    old = _released_script("v0.5.4")
    if old is None:
        pytest.skip("v0.5.4 is not in this clone's tags")
    host = Host(tmp_path, first_script=old)
    _git(host.work, "push", "-q", "-f", "origin", "v0.0.1:refs/heads/main")
    _git(host.work, "fetch", "-q", "origin")
    host.install("argus.service", "live", running=True)
    host.journal("argus.service", _spx_line(host))
    done = host.deploy()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "handing over to it" in done.stdout
    assert any(c.startswith("docker build") for c in host.calls())
    assert host.calls().count("systemctl try-restart argus.service") == 1


# -- the plan, the dry run, and the read-back's anchors -----------------------------


def test_a_dry_run_shows_each_units_plan_and_touches_nothing(host):
    """Every row on the table gets a line in the plan — installed or skipped — and
    an installed unit's own profile file is diffed between the tags. Nothing is
    checked out, built or restarted."""
    host.install("argus.service", "live", running=False)
    done = host.deploy("v0.0.2", "--dry-run")
    assert done.returncode == 0, done.stdout + done.stderr
    for row in host.rows:
        assert f"{row.unit}:" in done.stdout, row.unit
    assert "profile: live (argus.live.toml, sha " in done.stdout
    assert "restart: YES if running — a STOPPED unit stays stopped" in done.stdout
    assert "argus.live.toml: v0.0.1 -> v0.0.2" in done.stdout and "(no change)" in done.stdout
    assert _git(host.work, "describe", "--tags") == "v0.0.1"     # nothing checked out
    assert not [c for c in host.calls()
                if c.startswith(("docker build", "systemctl try", "VIOLATION"))]


def test_a_dry_run_shows_the_config_diff_of_an_installed_units_own_profile(host):
    """THE CONFIG DIFF IS THE REVIEW: on a config-only release that diff is the
    release. Shown for the installed unit's OWN profile — here the one whose file
    moved between the tags — and reported as a config change in its plan."""
    host.install("argus.service", MOVED_PROFILE, running=False)
    done = host.deploy("v0.0.2", "--dry-run")
    assert done.returncode == 0, done.stdout + done.stderr
    moved = host.row("argus.service").config(MOVED_PROFILE)
    assert f"{moved}: v0.0.1 -> v0.0.2" in done.stdout
    assert "+# 2" in done.stdout                                   # the diff itself
    assert "(no change)" not in done.stdout
    assert "config: YES" in done.stdout and f"           {moved}" in done.stdout
    assert _git(host.work, "describe", "--tags") == "v0.0.1"     # nothing checked out
    assert not [c for c in host.calls()
                if c.startswith(("docker build", "systemctl try", "VIOLATION"))]


def test_an_earlier_boots_traceback_does_not_fail_this_deploy(host):
    """The red-flag check reads the boot this deploy caused (`--since`), not the last N
    lines, which for an always-on process are mostly an earlier day's session."""
    host.install("argus.service", "live", running=True)
    (host.state / "journal-old-argus.service").write_text(
        "ERROR the session for 2026-10-09 failed\nTraceback (most recent call last):\n")
    host.journal("argus.service", _spx_line(host))
    done = host.deploy()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "journal looks clean" in done.stdout


def test_a_redeploy_of_a_tag_with_no_image_on_the_host_builds_it(tmp_path):
    """A deploy that stopped between its checkout and its build leaves the tree on the
    tag and no image for it. The re-run sees an empty diff; it must still build,
    never run the previous release's image under the new tag."""
    host = Host(tmp_path, start="v0.0.2")
    host.install("argus.service", "live", running=True)
    host.journal("argus.service", _spx_line(host))
    done = host.deploy("v0.0.2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "(no argus:v0.0.2 image on this host)" in done.stdout
    assert any(c.startswith("docker build") for c in host.calls())
    # And once the image exists, the same re-deploy restarts without rebuilding.
    (host.state / "calls").write_text("")
    done = host.deploy("v0.0.2")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(c.startswith("docker build") for c in host.calls())
    assert "systemctl try-restart argus.service" in host.calls()
