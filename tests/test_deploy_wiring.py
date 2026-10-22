"""The host wiring that selects a config file, pinned where nothing else can see it.

Three files decide which config a strategy's process loads — its systemd unit
(`argus.service`: the profile, via `EnvironmentFile`), its compose file
(`docker-compose.yml`: the interpolation and the mount), and
`scripts/argus-deploy.sh` (which verifies the result) — and **none of them is
Python**. A unit test of `config.resolve_config_path` proves the loader is right and
says nothing about whether the profile ever reaches it.

The invariant is the one the 2026-09-03 version-stamp near-miss taught, one layer
over: *prose and script are two implementations of one procedure, and only one of
them gets read by a human who can notice.* These assertions are deliberately
shallow — presence, absence, and shape — because their job is to fail when someone
tidies the fail-closed edges away, not to reimplement compose.

THE PLATFORM'S MODULE. It reads, at import, only the four files every checkout of
the platform carries (the SPX book's compose file and unit, the deploy script and
the Dockerfile), and everything it says about strategy units it derives from the
deploy script's own STRATEGY UNITS table (`deploy_rows`), so each assertion holds
whether that table has one row or several. The ancillary host units and the
operator docs are `test_deploy_wiring_host.py`'s; another strategy's own files are
pinned, by name, in that strategy's own module beside this one.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from fnmatch import fnmatch

from argus.config import CONFIG_SHA_LEN

_REPO = pathlib.Path(__file__).resolve().parents[1]
_COMPOSE = (_REPO / "docker-compose.yml").read_text()
_UNIT = (_REPO / "argus.service").read_text()
_DEPLOY = (_REPO / "scripts" / "argus-deploy.sh").read_text()
_DOCKERFILE = (_REPO / "Dockerfile").read_text()


def _uncommented(text: str) -> str:
    """Comments name the anti-patterns — `${ARGUS_ENV:-sandbox}` is written out in
    compose precisely to say never to write it. Reasoning is not configuration, so
    every assertion about what a file DOES is made against the instructions."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


_COMPOSE_DIRECTIVES = _uncommented(_COMPOSE)
_UNIT_DIRECTIVES = _uncommented(_UNIT)
_DOCKERFILE_INSTRUCTIONS = _uncommented(_DOCKERFILE)


# -- THE STRATEGY UNITS table, parsed once ---------------------------------------


@dataclass(frozen=True)
class DeployRow:
    """One row of THE STRATEGY UNITS table in `scripts/argus-deploy.sh`: one
    strategy process on the platform, as the deploy knows it."""

    unit: str              # its name on the host, e.g. argus.service (the override's default)
    unit_var: str          # the env override naming it, e.g. ARGUS_SERVICE
    unit_file: str         # the repo file it is installed from
    env_var: str           # the env override for its per-host profile file, e.g. ARGUS_HOST_ENV
    env_file: str          # that file's default path, e.g. /etc/argus/host.env
    prefix: str            # its config filename up to the profile, e.g. "argus."
    mounts: tuple[str, ...]  # the repo files its container bind-mounts, plus its compose file

    @property
    def compose(self) -> str:
        """The row's compose file: exactly one `.yml` among its mounts."""
        (compose,) = [m for m in self.mounts if m.endswith((".yml", ".yaml"))]
        return compose

    def config(self, profile: str) -> str:
        """The config file this row's unit loads under `profile`."""
        return f"{self.prefix}{profile}.toml"


def deploy_rows(script: str = _DEPLOY) -> list[DeployRow]:
    """THE STRATEGY UNITS table in argus-deploy.sh, row by row.

    Five parallel bash arrays (no associative arrays: the script also runs under
    macOS's bash 3.2) whose name and profile-file entries are `$VARIABLE`
    references, each resolved through the script's own `VARIABLE="${ARGUS_X:-
    default}"` line — so a row carries both the env override the tests set and the
    default a droplet gets. The one parser; every other assertion reads through it.
    """
    def array(name: str) -> list[str]:
        m = re.search(rf"^{name}=\((.*?)\)$", script, re.M | re.S)
        assert m, f"{name} not found in the deploy — has the table moved?"
        return re.findall(r'"([^"]*)"', m.group(1))

    defaults = {
        var: (env, default)
        for var, env, default in re.findall(
            r'^(\w+)="\$\{(ARGUS_\w+):-([^}]*)\}"$', script, re.M)
    }

    def resolve(ref: str) -> tuple[str, str]:
        assert ref.startswith("$") and ref[1:] in defaults, (
            f"{ref} is not a `VAR=\"${{ARGUS_X:-default}}\"` variable of the script")
        return defaults[ref[1:]]

    names = array("UNIT_NAMES")
    files = array("UNIT_FILES")
    envs = array("UNIT_ENV_FILES")
    prefixes = array("UNIT_CONFIG_PREFIXES")
    mounts = array("UNIT_MOUNTS")
    assert names and len(names) == len(files) == len(envs) == len(prefixes) == len(mounts), (
        "the five arrays of THE STRATEGY UNITS table must be parallel")
    rows = []
    for name, unit_file, env, prefix, mount_list in zip(names, files, envs, prefixes, mounts):
        unit_var, unit = resolve(name)
        env_var, env_file = resolve(env)
        rows.append(DeployRow(unit=unit, unit_var=unit_var, unit_file=unit_file,
                              env_var=env_var, env_file=env_file, prefix=prefix,
                              mounts=tuple(mount_list.split())))
    return rows


def _inert_patterns() -> set[str]:
    """The inert `case` PATTERNS of the deploy's change classifier, as tokens rather
    than as substrings — `discretionary_blackout_dates.txt` contains
    `blackout_dates.txt`, and a substring test would quietly pass on the one file
    that matters most."""
    block = _uncommented(
        _DEPLOY[_DEPLOY.index('INERT_CHANGES=""') : _DEPLOY.index("unit_reads() {")]
    )
    patterns = {
        pat.strip()
        for line in re.findall(r"^\s*([A-Za-z0-9_.*|/-]+)\)\s*$", block, re.M)
        for pat in line.split("|")
    }
    assert patterns, "no inert case arms found — has the classifier moved?"
    return patterns


def _unit_directives(text: str) -> set[str]:
    return {line.strip() for line in _uncommented(text).splitlines()
            if "=" in line and not line.startswith("[")}


# -- the image carries no answer to "which config?" ----------------------------


def test_the_image_bakes_no_config_and_no_config_path():
    """The `ENV ARGUS_CONFIG=/etc/argus/argus.toml` and the
    `COPY argus.toml /etc/argus/argus.toml` that shipped until v0.3.18 were an
    image-level default config — the shape v0.3.17 deleted from the code, one layer
    down — and the ENV half **wins over the profile**
    (`config.resolve_config_path`: an explicit path beats `ARGUS_ENV`), so it would
    have disarmed the mechanism silently.

    The baked COPY is the sharper one: it sat at the same path the bind-mount
    targets, so a drifted or missing mount fell back to it — and since both come
    from one git checkout the sha would MATCH, which makes it the single failure
    the config fingerprint cannot see. With nothing baked, a drifted mount is a
    hard `ConfigError` on every profile.
    """
    assert "ARGUS_CONFIG" not in _DOCKERFILE_INSTRUCTIONS, (
        "the image must carry no config path — it wins over ARGUS_ENV"
    )
    assert not re.search(r"^COPY\s+argus\..*\.toml", _DOCKERFILE_INSTRUCTIONS, re.M), (
        "no config may be baked into the image; the bind mount is the only source"
    )


# -- the profile reaches the container -----------------------------------------


def test_compose_refuses_rather_than_defaulting_the_profile():
    """`${ARGUS_ENV:?}`, never `${ARGUS_ENV:-sandbox}`.

    A default here is how the wrong config gets deployed quietly, which is the
    whole failure class the profile model exists to close — and it would be
    *silently* wrong on the one host where it matters, since the cutover IS the
    variable changing. An unset variable must be a loud stop.
    """
    assert "ARGUS_ENV: ${ARGUS_ENV:?" in _COMPOSE_DIRECTIVES, (
        "compose must interpolate ARGUS_ENV with `:?` so it refuses when unset"
    )
    assert "${ARGUS_ENV:-" not in _COMPOSE_DIRECTIVES, (
        "a default profile in compose defeats the point of naming one at all"
    )


def test_compose_does_not_set_an_explicit_config_path():
    """`ARGUS_CONFIG` WINS over the profile (`config.resolve_config_path`), so
    setting it here would leave `ARGUS_ENV` decorating a filename it no longer
    selects — and `/status` would report a profile the loader ignored."""
    assert not re.search(r"^\s*ARGUS_CONFIG:", _COMPOSE_DIRECTIVES, re.MULTILINE), (
        "compose must select the config by PROFILE, not by explicit path"
    )
    assert "ARGUS_CONFIG_DIR: /etc/argus" in _COMPOSE_DIRECTIVES


def test_the_mount_keeps_the_profile_in_the_filename():
    """Mounting to a fixed `/etc/argus/argus.toml` would work and would be one
    fewer moving part — and would disarm the filename<->environment assertion,
    because a profile-less name asserts nothing. The process could then no longer
    tell a live config from a cert one, nor report which it read."""
    assert (
        "./argus.${ARGUS_ENV}.toml:/etc/argus/argus.${ARGUS_ENV}.toml:ro"
        in _COMPOSE_DIRECTIVES
    )
    assert "./argus.toml:" not in _COMPOSE_DIRECTIVES, "the pre-rename mount is gone"


def test_the_unit_reads_the_profile_from_a_file_that_must_exist():
    """No `-` prefix, deliberately: a missing `host.env` must fail the UNIT rather
    than start it with the variable unset and let compose refuse one layer further
    from the cause. And not `./.env` — hand-edited, untouched by the deploy, and
    already the documented trap for `ARGUS_IMAGE_TAG`."""
    assert "EnvironmentFile=/etc/argus/host.env" in _UNIT_DIRECTIVES
    assert "EnvironmentFile=-" not in _UNIT_DIRECTIVES, (
        "a tolerated-missing EnvironmentFile turns a loud stop into a silent one"
    )


# -- one platform, several strategies: the table (v0.6.0) -----------------------
# docs/decisions/2026-10-22-one-platform-a-strategy-per-process.md


def test_the_strategy_units_table_has_the_spx_book_first_and_nothing_shared():
    """THE STRATEGY UNITS table is the deploy's whole knowledge of which processes
    run on the platform: one row per strategy, in five parallel arrays, and
    `deploy_rows` is the one parser of it. This pins the shape everything else here
    reads through.

    The first row is the SPX book's, by name: `argus.service`, its profile from
    `/etc/argus/host.env`, its config `argus.<profile>.toml`, its compose file and
    the blackout dates it bind-mounts. Any other row is another strategy: its own
    unit file, its own per-host profile file, a TWO-DOT config prefix
    (`argus.<strategy>.` — the loader's rule for a filename the SPX profile tests
    never claim) and its own compose file, sharing NOTHING with the first row or
    with each other — a file two rows list is read by two processes, and a release
    moving it would restart both. The script reads each row's profile from that
    row's own file, never from a shared one.
    """
    rows = deploy_rows()
    first = rows[0]
    assert first == DeployRow(
        unit="argus.service", unit_var="ARGUS_SERVICE", unit_file="argus.service",
        env_var="ARGUS_HOST_ENV", env_file="/etc/argus/host.env", prefix="argus.",
        mounts=("docker-compose.yml", "blackout_dates.txt"),
    )
    for row in rows:
        assert row.unit_file.endswith(".service") and row.unit == row.unit_file, row
        assert re.fullmatch(r"ARGUS_(\w+_)?SERVICE", row.unit_var), row
        assert re.fullmatch(r"ARGUS_(\w+_)?HOST_ENV", row.env_var), row
        assert row.env_file.startswith("/etc/argus/") and row.env_file.endswith(".env"), row
        assert row.compose, row                      # exactly one, or the property raises
        assert row.prefix.startswith("argus.") and row.prefix.endswith("."), row
    for row in rows[1:]:
        assert re.fullmatch(r"argus\.[a-z0-9_]+\.", row.prefix), (
            f"{row.unit}: another strategy's config is argus.<strategy>.<profile>.toml")
    fields = ("unit", "unit_file", "unit_var", "env_var", "env_file", "prefix", "compose")
    for field in fields:
        values = [getattr(r, field) for r in rows]
        assert len(set(values)) == len(values), f"rows share a {field}: {values}"
    for i, row in enumerate(rows):
        for other in rows[i + 1:]:
            assert not set(row.mounts) & set(other.mounts), (row.unit, other.unit)
    assert 'env_file="${UNIT_ENV_FILES[$i]}"' in _DEPLOY
    assert 'sudo cat "$env_file"' in _DEPLOY


# -- the deploy verifies what came up ------------------------------------------


def test_the_deploy_never_guesses_the_profile():
    """It reads, for each unit, the SAME file systemd reads for that unit, so it
    cannot verify against a different profile than the one that will start. Each
    row's profile file is overridable (the behaviour tests run the script against
    a throwaway host) but defaults to nothing but that row's own `/etc/argus/`
    file, and the config filename is the row's prefix plus the profile, computed
    in one place."""
    assert 'HOST_ENV_FILE="${ARGUS_HOST_ENV:-/etc/argus/host.env}"' in _DEPLOY
    assert 'U_CONFIG[$i]="${UNIT_CONFIG_PREFIXES[$i]}${PROFILE}.toml"' in _DEPLOY
    rows = deploy_rows()
    assert rows[0].env_file == "/etc/argus/host.env"
    for row in rows:
        assert f"{row.env_var}:-{row.env_file}" in _DEPLOY, row
        # And each unit's EnvironmentFile is the file its row names — with no `-`.
        unit = _uncommented((_REPO / row.unit_file).read_text())
        assert f"EnvironmentFile={row.env_file}" in unit, row
        assert "EnvironmentFile=-" not in unit, row


def test_the_deploy_reads_the_running_config_back():
    """The version stamp's other half, and the ONLY post-restart assertion a
    config-only release has: on one of those the image is deliberately untouched,
    so the version legitimately still names the previous tag and the version
    read-back is correctly skipped. Before this, that class verified nothing."""
    assert "EXPECTED_CONFIG_SHA" in _DEPLOY
    assert "RUNNING_CONFIG_SHA" in _DEPLOY
    assert re.search(
        r'\[ "\$RUNNING_CONFIG_SHA" != "\$EXPECTED_CONFIG_SHA" \]', _DEPLOY
    ), "the deploy must COMPARE the two, not merely print them"


def test_the_config_read_back_is_not_gated_on_having_built():
    """The version read-back sits inside `if [ "$NEED_BUILD" -eq 1 ]`, correctly.
    The config read-back must not: a config-only release is exactly the case it
    exists for, and inheriting that guard would make it inert on the only class
    that needs it.
    """
    guard = _DEPLOY.index(
        'if [ "$NEED_BUILD" -eq 1 ] && [ "$restarted" -eq 1 ]; then\n'
        '        if [ -z "$RUNNING_VERSION" ]')
    assert _DEPLOY.index('RUNNING_CONFIG_LINE="$(') > guard, (
        "the config read-back must come AFTER the version block, not inside it"
    )
    tail = _DEPLOY[guard:]
    # The version block ends at its own `fi`; everything the config check needs is
    # past it, at top level.
    assert "RUNNING_CONFIG_LINE" in tail.split("\n    fi\n", 1)[1]


def test_the_read_back_matches_the_platforms_startup_line():
    """The deploy's only post-restart evidence is one journal line, and the process
    writes it: `argus build <version> config <profile>/<filename> @<sha>` (`app.py`
    for the SPX book; every strategy on the platform logs the same line). Two
    implementations of one line, like the fingerprint below: the script's regexes
    and its awk field numbers are pinned against the format string, so a reworded
    line cannot leave the deploy warning "no config fingerprint" on every release.
    """
    app = (_REPO / "src" / "argus" / "app.py").read_text()
    fmt = "argus build %s config %s/%s @%s"
    assert f'"{fmt}"' in app, "app.py no longer logs the startup line the deploy reads"
    assert "grep -oE 'argus build [^[:space:]]+ config [^[:space:]]+ @[^[:space:]]+'" in _DEPLOY
    assert "grep -oE 'argus build [^[:space:]]+'" in _DEPLOY
    fields = (fmt % ("v9.9.9", "live", "argus.live.toml", "abc123def456")).split()
    assert fields[2] == "v9.9.9"                    # awk '{print $3}': the version
    assert fields[4] == "live/argus.live.toml"      # awk '{print $5}': profile/filename
    assert fields[5] == "@abc123def456"             # awk '{print $6}' | tr -d '@': the sha
    assert "| tail -n 1 | awk '{print $3}'" in _DEPLOY
    assert "RUNNING_CONFIG=\"$(echo \"$RUNNING_CONFIG_LINE\" | awk '{print $5}')\"" in _DEPLOY
    assert "RUNNING_CONFIG_SHA=\"$(echo \"$RUNNING_CONFIG_LINE\" | awk '{print $6}' | tr -d '@')\"" in _DEPLOY
    assert '[ "$RUNNING_CONFIG" != "${PROFILE}/${CONFIG_FILE}" ]' in _DEPLOY, (
        "the profile and filename are compared too, not only the sha")
    # The good sign the SPX book logs once its session is up.
    good = re.search(r"^GOOD_SIGNS='([^']*)'$", _DEPLOY, re.M).group(1).split("|")
    assert "argus started in" in good
    assert '"argus started in %s mode"' in app


def test_the_deploy_hands_over_to_the_script_the_tag_ships():
    """The script is itself deployed BY the release, so the script deploying
    v<N+1> is the one from v<N> — it runs out of the working tree, and the tree is
    on the old tag until its own checkout.

    That is not theoretical: **v0.3.18's own deploy failed on it.** The release
    made `ARGUS_ENV` mandatory for every `docker compose` invocation; v0.3.17's
    script did not pass it, and died on compose's `${ARGUS_ENV:?}` against the new
    compose file it had just checked out — half-deployed, with the old unit still
    installed. Everything after the checkout belongs to the release being
    deployed, so the release's own script must run it.

    `ARGUS_DEPLOY_PREV_TAG` is the part that is easy to get wrong: without it the
    handed-over run re-derives `PREV_TAG` from a tree already on `$VERSION`, sees
    an empty diff, and silently skips a build the first run had correctly planned.
    """
    assert "exec env ARGUS_DEPLOY_REEXECED=1" in _DEPLOY, (
        "the deploy must hand over to the script the tag ships"
    )
    assert 'PREV_TAG="${ARGUS_DEPLOY_PREV_TAG:-' in _DEPLOY, (
        "the hand-over must carry PREV_TAG, or the second run plans no build"
    )
    # Before the checkout and after it — the comparison is the whole mechanism.
    assert _DEPLOY.index("SELF_SHA_BEFORE=") < _DEPLOY.index('git checkout "$VERSION"')
    assert _DEPLOY.index("SELF_SHA_AFTER=") > _DEPLOY.index('git checkout "$VERSION"')


def test_every_compose_invocation_in_the_deploy_carries_the_profile():
    """The concrete v0.3.18 failure, pinned: compose interpolates the config mount
    with `${ARGUS_ENV:?}` and refuses when unset — on `build` and `run`, not only
    `up` — and **sudo scrubs the environment**, so it has to be on sudo's own
    command line rather than exported.

    Since v0.6.0 the deploy runs NO compose command: the image is the platform's,
    built once by `docker build`, and each unit runs its own compose project. Any
    compose line added later still has to carry its unit's profile.
    """
    joined = _DEPLOY.replace("\\\n", " ")
    for line in joined.splitlines():
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        if "docker compose" not in code:
            continue
        assert 'ARGUS_ENV="$PROFILE"' in code, (
            f"compose invocation without the profile:\n  {code.strip()}\n"
            "compose refuses when ARGUS_ENV is unset, and sudo scrubs the env."
        )
    assert re.search(r'sudo docker build --build-arg "ARGUS_VERSION=\$\{ACTUAL\}"', joined), (
        "the deploy builds the platform's one image itself, stamped with the tag")
    assert '-t argus:local -t "argus:${VERSION}"' in joined


def test_the_deploy_and_the_loader_compute_the_same_fingerprint():
    """Two implementations of one number, and the coupling is invisible from
    either side: the script cuts a fixed width out of a sha256, the loader slices
    `CONFIG_SHA_LEN`. Move the constant and the read-back starts asserting a
    running config against a prefix nothing will ever report."""
    assert f"cut -c1-{CONFIG_SHA_LEN}" in _DEPLOY, (
        f"the deploy must take {CONFIG_SHA_LEN} hex chars, matching CONFIG_SHA_LEN"
    )
    assert "sha256sum" in _DEPLOY


def test_nothing_compose_bind_mounts_is_treated_as_inert_by_the_deploy():
    """The deploy skips the restart when every changed path is affirmatively inert
    — and "inert" means exactly *the running process cannot read it*. What it CAN
    read is enumerated in one place and one place only: its compose file's bind
    mounts, plus the image and the systemd unit.

    So the two files have to agree, and nothing else can notice if they stop.
    Bind-mount a new repo file without removing it from the script's inert list and
    a release changing that file would deploy with **no restart and no error** —
    the process still reading the old contents, the deploy reporting success. And
    the row's mounts have to be the compose file's, exactly: a mount the row lists
    and the compose file does not is a row that has drifted from what its process
    reads.
    """
    inert_patterns = _inert_patterns()
    # Each unit's own config falls through to visible for THAT unit only.
    assert '[ "$path" = "${U_CONFIG[$row]}" ]' in _DEPLOY

    for row in deploy_rows():
        directives = _uncommented((_REPO / row.compose).read_text())
        mounts = re.findall(r"^\s*-\s*\./([^:]+):", directives, re.M)
        assert mounts, f"no bind mounts found in {row.compose} — has the file changed shape?"
        data_mounts = set()
        for mount in mounts:
            if "${ARGUS_ENV}" in mount:
                # The config mount, matched in the script by the row's own config name.
                assert mount == f"{row.prefix}${{ARGUS_ENV}}.toml", (
                    f"{row.compose} mounts {mount}, but its unit's row resolves "
                    f"{row.prefix}<profile>.toml")
                continue
            # fnmatch, not equality: a glob arm (`*.txt)`) swallows a mounted file
            # just as effectively as naming it, and is easier to add unnoticed.
            hit = [p for p in inert_patterns if fnmatch(mount, p)]
            assert not hit, (
                f"{mount} is bind-mounted into the container but the deploy treats it "
                f"as inert (matched by {hit}) — a release changing it would skip the "
                "restart silently"
            )
            assert mount in row.mounts, (
                f"{row.compose} bind-mounts {mount}, but {row.unit}'s row in the deploy "
                "(UNIT_MOUNTS) does not list it: another unit could not tell it is not "
                "theirs, and this one's plan would not report it as config")
            data_mounts.add(mount)
        assert data_mounts == set(row.mounts) - {row.compose}, (
            f"{row.unit}'s row lists mounts its compose file does not make")


def test_no_strategy_unit_is_treated_as_inert_by_the_deploy():
    """A strategy's unit file is bind-read by systemd for the profile, and a
    re-synced unit only takes effect on a restart — so every row's unit must stay
    VISIBLE. Checked by fnmatch against each row, not by name: a `*.service` glob
    among the inert arms would swallow every unit on the table at once."""
    inert_patterns = _inert_patterns()
    for row in deploy_rows():
        hit = [p for p in inert_patterns if fnmatch(row.unit_file, p)]
        assert not hit, (
            f"{row.unit_file} must stay VISIBLE — it is bind-read by systemd for the "
            f"profile and a re-synced unit only takes effect on a restart (matched {hit})")


def test_the_plan_names_the_release_class():
    """An operator reading the plan should SEE which class this is. Deducing
    "config-only" from the absence of a build is how a release gets read as a
    no-op — and a config-only release is the one whose whole content is a diff.

    So the config class must be COMPLETE per row: every file a row bind-mounts,
    and every profile file its prefix names in the repo. A profile file left off
    the list makes a release moving only it print "nothing in src/, deps, or the
    Dockerfile moved" — the v0.3.19 misnaming, in the other direction.
    """
    assert "CONFIG_INPUTS=(" in _DEPLOY
    for named in ("argus.sandbox.toml", "argus.live.toml", "blackout_dates.txt"):
        assert named in _DEPLOY, f"{named} must count as a config input"
    assert 'echo "    config: YES"' in _DEPLOY
    inputs = set(
        re.search(r"^CONFIG_INPUTS=\((.*?)^\)$", _DEPLOY, re.M | re.S).group(1).split())
    for row in deploy_rows():
        for mount in row.mounts:
            assert mount in inputs, f"{mount} ({row.unit}) must count as a config input"
        profile_files = sorted(
            p.name for p in _REPO.glob(f"{row.prefix}*.toml")
            if re.fullmatch(rf"{re.escape(row.prefix)}[a-z]+\.toml", p.name))
        assert profile_files, f"no profile files for {row.unit} ({row.prefix}<profile>.toml)"
        for name in profile_files:
            assert name in inputs, f"{name} ({row.unit}) must count as a config input"


# -- one platform, several strategies: the units (v0.6.0) -----------------------


def _code_lines(text: str) -> list[str]:
    joined = text.replace("\\\n", " ")
    return [line.split("#", 1)[0] for line in joined.splitlines()
            if not line.lstrip().startswith("#")]


def test_the_deploy_never_starts_a_strategy():
    """`systemctl restart` STARTS a stopped unit, and until v0.6.0 it was the deploy's
    last step: any deploy would have brought the killed SPX book (stopped, disabled,
    kill switch engaged since 2026-10-01) back up. `try-restart` restarts a running
    unit and does nothing to a stopped one. Starting a strategy is a person's act."""
    # Lines the script RUNS: not comments, and not the advice it prints (the plan
    # tells an operator how to re-enable an ancillary TIMER by hand).
    code = "\n".join(line for line in _code_lines(_DEPLOY)
                     if not line.lstrip().startswith("echo "))
    for verb in ("restart", "start", "enable", "reenable", "unmask"):
        assert not re.search(rf"systemctl\s+{verb}\b", code), (
            f"the deploy runs `systemctl {verb}`: it must never start a strategy unit")
    assert 'sudo systemctl try-restart "$unit"' in code


def test_every_strategy_unit_in_the_deploy_exists_in_the_repo():
    for row in deploy_rows():
        assert (_REPO / row.unit_file).is_file(), row.unit_file
        for mount in row.mounts:
            assert (_REPO / mount).is_file(), mount


def test_the_spx_compose_project_names_no_other_strategys_files():
    """Each compose project mounts its own row's files and nothing of another's: a
    file two projects mount is read by two processes, and a release moving it would
    restart both. With one row on the table there is nothing to check."""
    first, *others = deploy_rows()
    for row in others:
        for name in (row.unit_file, row.prefix, row.env_file, *row.mounts,
                     row.unit.removesuffix(".service")):
            assert name not in _COMPOSE_DIRECTIVES, (row.unit, name)


def test_every_other_strategys_compose_project_is_its_own():
    """Another strategy's compose file, held to the SPX book's shape (the rules the
    tests above pin for `docker-compose.yml`) and to its own project: its own name
    (both files sit in /opt/argus, and without one they would share the project
    `argus`, so `down --remove-orphans` from either removes both); the platform's
    image, built once by the deploy, never built or pulled here; which strategy (a
    literal, its identity) and which profile (per host, refused rather than
    defaulted), resolved by profile, never by an explicit path; its own store, the
    SPX book's audit databases never reachable from it; host networking and the
    credentials only at runtime; every repo file it reads mounted read-only."""
    first, *others = deploy_rows()
    for row in others:
        d = _uncommented((_REPO / row.compose).read_text())
        project = row.unit.removesuffix(".service")
        assert re.search(rf"^name: {re.escape(project)}$", d, re.M), (row.compose, project)
        assert "image: argus:${ARGUS_IMAGE_TAG:-local}" in d
        assert "pull_policy: never" in d
        assert not re.search(r"^\s*build:", d, re.M)
        assert re.search(r"^\s*ARGUS_STRATEGY: [a-z][a-z0-9_]*$", d, re.M), (
            f"{row.compose} must name its strategy as a literal")
        assert "ARGUS_ENV: ${ARGUS_ENV:?" in d and "${ARGUS_ENV:-" not in d
        assert "ARGUS_CONFIG_DIR: /etc/argus" in d
        assert not re.search(r"^\s*ARGUS_CONFIG:", d, re.M)
        assert (f"./{row.prefix}${{ARGUS_ENV}}.toml:/etc/argus/{row.prefix}${{ARGUS_ENV}}.toml:ro"
                in d)
        assert f"{project}-state:/data" in d
        assert "argus-state" not in d
        assert "network_mode: host" in d
        assert re.search(r"env_file:\s*\n\s*- \.env", d)
        for mount in re.findall(r"^\s*-\s*(\./\S+)", d, re.M):
            assert mount.endswith(":ro"), f"{mount} must be read-only"


def test_every_other_strategys_unit_is_argus_service_but_for_its_own_project():
    """argus.service line for line: the same supervision, the same fail-closed
    profile file, differing only in what names the project — its description and
    documentation, its EnvironmentFile (the row's own), its compose file in
    ExecStart and ExecStop, and its syslog identifier. Any other directive that
    differs is a second supervision policy nobody decided on."""
    first, *others = deploy_rows()
    spx = _unit_directives(_UNIT)
    own = {"Description", "Documentation", "EnvironmentFile", "ExecStart", "ExecStop",
           "SyslogIdentifier"}
    for row in others:
        other = _unit_directives((_REPO / row.unit_file).read_text())
        differing = {line.split("=", 1)[0] for line in spx ^ other}
        assert differing <= own, (row.unit, differing - own)
        assert f"EnvironmentFile={row.env_file}" in other
        assert "EnvironmentFile=-" not in other
        assert f"ExecStart=/usr/bin/docker compose -f /opt/argus/{row.compose} up" in other
        assert f"ExecStop=/usr/bin/docker compose -f /opt/argus/{row.compose} down" in other
        assert f"SyslogIdentifier={row.unit.removesuffix('.service')}" in other
