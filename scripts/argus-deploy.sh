#!/usr/bin/env bash
#
# Deploys (or rolls back to) a tagged Argus release on the droplet. Automates
# the private deployment runbook's §17 checkout/verify/build/tag/restart
# sequence so the steps that have been skipped by hand — the version-tag step
# (missing from every deploy before 2026-08-19) and the checkout verification
# (the step that would have caught the 2026-08-05 no-op-checkout incident) —
# cannot be skipped again.
#
# ONE PLATFORM, SEVERAL STRATEGIES (v0.6.0;
# docs/decisions/2026-10-22-one-platform-a-strategy-per-process.md). The release builds ONE image, and every strategy runs it
# as its own process under its own systemd unit: the SPX book (`argus.service`)
# and any other strategy's unit, listed in THE STRATEGY UNITS below.
# For each unit INSTALLED on this host the deploy reads its profile, checks the
# tag carries its config, syncs its unit file, restarts it if the release moved
# anything it reads, and reads its startup line back. **It never STARTS one:** a
# stopped unit stays stopped (`systemctl try-restart`). Until v0.6.0 the last step
# was `systemctl restart argus.service`, which starts a stopped unit, so deploying
# anything at all would have brought the killed SPX book back up.
#
# THE SAME COMMAND IS THE ROLLBACK. Pass any previously-released tag instead of
# the newest one and this rebuilds, retags, and restarts that exact version —
# the "checkout the previous release and rebuild" fallback, just no longer
# manual. It does not depend on `ARGUS_IMAGE_TAG` or a previously-tagged image
# still existing: the build is deterministic (base image pinned by digest,
# dependencies pinned by hash — see Dockerfile), so it reproduces byte-identical
# behaviour from source even if that release's image was long since overwritten.
#
# Usage:
#   scripts/argus-deploy.sh v0.3.5              # deploy (or roll back to) v0.3.5
#   scripts/argus-deploy.sh --dry-run v0.3.5    # show the plan, touch nothing
#   scripts/argus-deploy.sh --force-build v0.3.5  # rebuild even if src/deps look unchanged
#   scripts/argus-deploy.sh --restart v0.3.5      # restart even if nothing the process reads moved
#
# A release that moves only scripts/, docs/, tests/ or a file another unit reads
# does not restart a unit: its process cannot see any of it, and a restart tears
# down the session, the watchdog, the virtual stops and the marks (the SPX book)
# or another strategy's own session. Pass --restart to force one anyway — e.g. to recover a
# crashed container. Re-deploying the tag already checked out restarts every
# RUNNING unit unconditionally, which is that same recovery path.
#
# Run as the deploy user (deploy-user), WITHOUT sudo on the script itself — it
# calls sudo internally only for docker/systemctl/journalctl and one `cat` of
# each unit's profile file (root-owned, 600), and NEVER for git
# (CLAUDE.md / the runbook: the deploy key belongs to deploy-user, not root; a
# `sudo git` either fails on the network calls or leaves root-owned objects in
# .git that later break a plain-user `git` write).
#
# The build is STAMPED with the release tag (`--build-arg ARGUS_VERSION`), which is
# what `GET /status` and the startup log report, and the deploy READS IT BACK from
# each restarted unit's journal afterwards. That last step is the only check that
# catches a restart which quietly reattached to the previous image — a unit has
# no build step, so that failure is otherwise completely silent.
#
# THE CONFIG IS READ BACK THE SAME WAY, and on EVERY release class. Config is
# bind-mounted and deliberately does not roll back with the image tag, so the
# version read-back says nothing about it — and on a config-only release the
# version legitimately still names the PREVIOUS tag, which is why that check is
# skipped there. Until the sha read-back existed, a config-only release verified
# nothing at all: "the new config loaded", "the restart beat the checkout", "the
# mount path drifted" and "somebody hand-edited the droplet and it has not
# restarted since" all looked identical.
#
# --dry-run touches NOTHING beyond `git fetch --tags` (safe, idempotent): no
# checkout, no build, no restart. It reads the target tag by ref and diffs
# against the currently-checked-out one without moving the working tree, so
# it is safe to run against a real upcoming release before you trust it. (It
# runs THIS copy of the script, the one checked out now; the release's own copy
# takes over only after a real checkout.)
#
# Overridable via env:
#   ARGUS_REPO_DIR        (default /opt/argus)
#   ARGUS_SERVICE         (default argus.service — the SPX book's unit)
#   ARGUS_HOST_ENV        (default /etc/argus/host.env — its profile, per host)
#   ARGUS_SYSTEMD_DIR     (default /etc/systemd/system — where an installed unit is)
#   ARGUS_DEPLOY_SETTLE_S (default 8 — seconds a restarted process gets to report in)

set -euo pipefail

REPO_DIR="${ARGUS_REPO_DIR:-/opt/argus}"
SERVICE="${ARGUS_SERVICE:-argus.service}"
HOST_ENV_FILE="${ARGUS_HOST_ENV:-/etc/argus/host.env}"
SYSTEMD_DIR="${ARGUS_SYSTEMD_DIR:-/etc/systemd/system}"
SETTLE_S="${ARGUS_DEPLOY_SETTLE_S:-8}"

# --- THE STRATEGY UNITS ---------------------------------------------------------
#
# One row per strategy process on the platform, in parallel arrays (no associative
# arrays: this also runs under macOS's bash 3.2, off-droplet and in the tests):
#   - the systemd unit, and the repo file it is installed from;
#   - the per-host file naming its PROFILE (one line, ARGUS_ENV=<profile>; the
#     unit's EnvironmentFile, root-owned 600, never in git);
#   - its config filename up to the profile: argus.<profile>.toml for the SPX book,
#     argus.<strategy>.<profile>.toml for any other strategy;
#   - the repo files its container bind-mounts besides its config, plus its compose
#     file. Those are read by THAT unit's process and by no other.
#
# Every other repo file is either inert to every unit (the case arms below) or read
# by every unit: the image's inputs, and anything nobody classified (fail-closed).
# A NEW STRATEGY IS A NEW ROW HERE, and `tests/test_deploy_wiring.py` holds each
# row's mounts to its compose file.
UNIT_NAMES=("$SERVICE")
UNIT_FILES=("argus.service")
UNIT_ENV_FILES=("$HOST_ENV_FILE")
UNIT_CONFIG_PREFIXES=("argus.")
UNIT_MOUNTS=(
    "docker-compose.yml blackout_dates.txt"
)

DRY_RUN=0
FORCE_BUILD=0
FORCE_RESTART=0
VERSION=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --force-build) FORCE_BUILD=1 ;;
        --restart) FORCE_RESTART=1 ;;
        -h|--help)
            sed -n '2,78p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        v[0-9]*.*)
            VERSION="$arg"
            ;;
        *)
            echo "argus-deploy: unrecognized argument: $arg" >&2
            exit 2
            ;;
    esac
done

if [ -z "$VERSION" ]; then
    echo "argus-deploy: usage: $0 [--dry-run] [--force-build] [--restart] v<X.Y.Z>" >&2
    exit 2
fi
if ! [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "argus-deploy: '$VERSION' doesn't look like a release tag (want vX.Y.Z)" >&2
    exit 2
fi

# `sudo git` is a documented incident, not a hypothetical — see CLAUDE.md /
# runbook §17. Running the WHOLE script under sudo would make every git call
# below run as root, so refuse outright rather than let it happen by habit.
if [ "$(id -u)" -eq 0 ]; then
    echo "argus-deploy: do not run this with sudo. It calls sudo itself only" >&2
    echo "for docker/systemctl/journalctl and reading each unit's profile file." >&2
    echo "Run it as the deploy user." >&2
    exit 2
fi

# The config fingerprint, computed exactly as `Config.load` computes it: sha256
# over the file BYTES, first 12 hex (argus/config.py, CONFIG_SHA_LEN). Two
# implementations of one number is a coupling nothing else can see, so
# `tests/test_deploy_wiring.py` pins it: if that constant moves, both ends must.
_sha12() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -c1-12
    else
        shasum -a 256 | cut -c1-12   # macOS, for running this script off-droplet
    fi
}

# THIS SCRIPT IS ITSELF DEPLOYED BY THE RELEASE, so the script that deploys
# v<N+1> is the one from v<N> — it runs out of the working tree, and the working
# tree is on the OLD tag until the checkout further down. That bites the
# moment a release changes what the steps AFTER the checkout must do:
# **v0.3.18 required every `docker compose` invocation to carry `ARGUS_ENV`, and
# v0.3.17's script did not pass it, so the build died on compose's own
# `${ARGUS_ENV:?}` — against the new compose file it had just checked out.**
# (Recovered by hand; that deploy is the reason this block exists.) And v0.6.0's
# deploy is SAFE only because of it: v0.5.4's copy ends in `systemctl restart`,
# but it hands over to this one after the checkout, before any build or restart.
#
# So: remember what this file was before the checkout, and if the checkout
# replaced it, hand over to the new one. That also removes a second, quieter
# hazard — bash reads a script INCREMENTALLY, so a checkout that rewrites the file
# mid-run can make the remaining lines come from a different version at an
# arbitrary byte offset.
#
# Resolved to an ABSOLUTE path here, before the `cd` below: `$0` is relative to
# the invoking directory, and `ARGUS_REPO_DIR` may point somewhere else entirely.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
SELF_SHA_BEFORE="$(_sha12 < "$SELF")"

cd "$REPO_DIR"

# WHICH CONFIG PROFILE EACH INSTALLED UNIT RUNS. Read from the SAME file systemd
# reads for that unit, so the script cannot deploy against a different profile
# than the one that will start — never defaulted, never inferred from the branch,
# never guessed. A default here would be the same failure the compose files'
# `${ARGUS_ENV:?}` exists to prevent, one layer up.
#
# A unit that is NOT INSTALLED on this host is skipped, and said so: installing a
# strategy's unit is a one-time, deliberate step (its runbook), never a deploy's.
U_INSTALLED=()
U_PROFILE=()
U_CONFIG=()
for i in "${!UNIT_NAMES[@]}"; do
    unit="${UNIT_NAMES[$i]}"
    U_PROFILE[$i]=""
    U_CONFIG[$i]=""
    if [ ! -f "${SYSTEMD_DIR}/${unit}" ]; then
        U_INSTALLED[$i]=0
        continue
    fi
    U_INSTALLED[$i]=1
    env_file="${UNIT_ENV_FILES[$i]}"
    if ! PROFILE_LINE="$(sudo cat "$env_file" 2>/dev/null)"; then
        echo "argus-deploy: ${unit} is installed but cannot read ${env_file}." >&2
        echo "argus-deploy: that unit has no config profile, so it cannot start" >&2
        echo "(its EnvironmentFile has no '-' prefix, deliberately)." >&2
        echo "argus-deploy: provision it first — the runbook for that unit." >&2
        exit 1
    fi
    # Matched exactly as systemd parses an EnvironmentFile — `NAME=value`, optional
    # leading whitespace, optional quotes, NO spaces around the `=`. Being more
    # lenient than systemd here would be worse than being wrong: the script would
    # resolve a profile the unit then fails to set, and the two readers of one file
    # would disagree silently. `#` and `;` comment lines fall out of the same anchor.
    PROFILE="$(printf '%s\n' "$PROFILE_LINE" \
        | sed -n 's/^[[:space:]]*ARGUS_ENV="\{0,1\}\([A-Za-z0-9_-]\{1,\}\)"\{0,1\}[[:space:]]*$/\1/p' \
        | tail -n 1)"
    if [ -z "$PROFILE" ]; then
        echo "argus-deploy: ${env_file} carries no usable ARGUS_ENV line." >&2
        echo "argus-deploy: it must contain exactly one, e.g. 'ARGUS_ENV=live'." >&2
        exit 1
    fi
    U_PROFILE[$i]="$PROFILE"
    U_CONFIG[$i]="${UNIT_CONFIG_PREFIXES[$i]}${PROFILE}.toml"
done

echo "argus-deploy: fetching tags ..."
git fetch --tags

# If the droplet has been hand-edited and committed to, a checkout would orphan
# those commits silently. Refuse rather than guess.
#
# Asked ONCE, before the checkout. After a hand-over HEAD is the release tag itself,
# which need not be on origin/main (a tag pushed before its branch), and asking
# again there would abort a deploy that is already half done: checked out, not yet
# built (found reviewing v0.6.0).
if [ "${ARGUS_DEPLOY_REEXECED:-0}" = "1" ]; then
    AHEAD=""
else
    AHEAD="$(git log --oneline "origin/main..HEAD" 2>/dev/null || true)"
fi
if [ -n "$AHEAD" ]; then
    echo "argus-deploy: HEAD has commits origin/main does not:" >&2
    echo "$AHEAD" >&2
    echo "argus-deploy: the droplet was committed to by hand. Resolve this" >&2
    echo "before deploying — a checkout here would silently orphan those commits." >&2
    exit 1
fi

# Uncommitted local edits, checked BEFORE checkout so they are never silently
# carried over or clobbered by it.
DIRTY_BEFORE="$(git status --short)"
if [ -n "$DIRTY_BEFORE" ]; then
    echo "argus-deploy: working tree is not clean:" >&2
    echo "$DIRTY_BEFORE" >&2
    echo "argus-deploy: inspect and resolve (commit, stash, or discard) before deploying." >&2
    exit 1
fi

if ! git rev-parse -q --verify "refs/tags/${VERSION}" >/dev/null; then
    echo "argus-deploy: tag ${VERSION} not found after fetch — check the spelling" \
         "and that it's pushed." >&2
    exit 1
fi

# What is currently deployed, for the diff below. Empty if the droplet somehow
# isn't sitting on a tag (unexpected, but not fatal — treated as "unknown",
# which biases every decision below toward doing more, not less).
#
# `ARGUS_DEPLOY_PREV_TAG` is set ONLY by the hand-over below, and it is not
# cosmetic: after a hand-over the tree is already on $VERSION, so a fresh
# `git describe` would answer $VERSION, `git diff $VERSION $VERSION` would be
# empty, and the new script would conclude "config-only" and SKIP THE BUILD the
# old one had correctly planned.
PREV_TAG="${ARGUS_DEPLOY_PREV_TAG:-$(git describe --tags 2>/dev/null || true)}"

if [ "$PREV_TAG" = "$VERSION" ]; then
    echo "argus-deploy: already on ${VERSION} — proceeding anyway (idempotent;" \
         "useful for recovering a crashed container without a version change)."
fi

# A ROLLBACK PAST v0.6.0 WOULD START THE STOPPED SPX BOOK. The hand-over below gives
# the target tag's OWN script the build and the restart, and every script before
# v0.6.0 ends in `systemctl restart ${SERVICE}`, which starts a stopped unit. So a
# target whose script has no `try-restart` is refused while that unit is not running,
# BEFORE anything is checked out — the dry run says so too. Roll such a tag back by
# hand (runbook §17's manual sequence, without its restart), or start the unit first
# if running it is genuinely intended.
#
# Read whole, then matched: `git show | grep -q` under `pipefail` fails on a MATCH,
# because grep exits at the first hit and git dies of SIGPIPE.
TARGET_SCRIPT="$(git show "${VERSION}:scripts/argus-deploy.sh" 2>/dev/null || true)"
case "$TARGET_SCRIPT" in
    *"systemctl try-restart"*) TARGET_NEVER_STARTS=1 ;;
    *) TARGET_NEVER_STARTS=0 ;;
esac
if [ "$TARGET_NEVER_STARTS" -eq 0 ] \
        && [ -f "${SYSTEMD_DIR}/${SERVICE}" ] \
        && ! sudo systemctl is-active --quiet "$SERVICE"; then
    echo "argus-deploy: ${VERSION}'s own deploy script predates v0.6.0: it ends in" >&2
    echo "\`systemctl restart ${SERVICE}\`, which would START that stopped unit (the SPX" >&2
    echo "book, stopped and disabled, its kill switch engaged). Refusing before any" >&2
    echo "checkout. Roll back by hand without the restart, or start the unit first if" >&2
    echo "that is truly intended." >&2
    exit 1
fi

# Which of the two deploy classes this is (runbook §17's distinction, decided
# HERE rather than by memory). A config-only release needs no rebuild — each
# unit's config, its other bind mounts and its compose file are all read from
# disk, not baked into the image. Anything touching the image's own inputs does.
BUILD_INPUTS=(src requirements.txt pyproject.toml Dockerfile)
# The other class, NAMED rather than inferred from the absence of a build. An
# operator reading the plan should see which class this is; deducing "config-only"
# from "build: no" is how a release gets read as a no-op.
CONFIG_INPUTS=(
    argus.dev.toml argus.sandbox.toml argus.live.toml
    blackout_dates.txt discretionary_blackout_dates.txt fomc_dates.txt
    docker-compose.yml
)
if [ -n "$PREV_TAG" ]; then
    CHANGED_BUILD_INPUTS="$(git diff --name-only "$PREV_TAG" "$VERSION" -- "${BUILD_INPUTS[@]}")"
    CHANGED_CONFIG_INPUTS="$(git diff --name-only "$PREV_TAG" "$VERSION" -- "${CONFIG_INPUTS[@]}")"
else
    # Unknown prior state: don't guess. Rebuild and resync every unit file.
    CHANGED_BUILD_INPUTS="(previous tag unknown — building unconditionally)"
    CHANGED_CONFIG_INPUTS="(previous tag unknown)"
fi

# --- CAN A RUNNING PROCESS SEE ANY OF THIS? -------------------------------------
#
# There is a THIRD shape, and until v0.3.20 the plan had no name for it: a release
# that moves only the deploy script, the docs, or the tests. v0.3.19 was exactly
# that, and it reported "skipping build (config-only release)" — naming the wrong
# class — and then restarted the trading process for a change the process cannot
# read. A restart is not free: it tears down the session, the watchdog, the virtual
# stops and the marks, and it is the one deploy step with a timing rule attached.
#
# So the question the plan answers is not "which class is this?" but **"is there
# anything here this unit's process can see?"** — and it is answered FAIL-CLOSED,
# by elimination: a changed path must be affirmatively recognized as inert to every
# unit (the arms below) or as another unit's own file (`unit_reads`), or the
# restart happens. A file class nobody thought of restarts, which is the direction
# that costs a session rather than a rail.
INERT_CHANGES=""
CANDIDATE_CHANGES=""
ANCILLARY_UNIT_CHANGES=""
UNKNOWN_PREV=0
if [ -n "$PREV_TAG" ] && [ "$PREV_TAG" != "$VERSION" ]; then
    while IFS= read -r changed; do
        [ -z "$changed" ] && continue
        case "$changed" in
            # Not in the image, not bind-mounted, not read at startup. `scripts/`
            # IS the deploy rather than an input to it, and by the time it could
            # matter the hand-over above has already run it.
            scripts/*|docs/*|tests/*|README.md|CLAUDE.md|.gitignore)
                INERT_CHANGES="${INERT_CHANGES}${changed} "
                continue
                ;;
            # Generator inputs. `tools.blackout_dates` reads them to PRODUCE
            # blackout_dates.txt; the running process reads only the product, and
            # docker-compose.yml bind-mounts only the product. THE RULE: a
            # container can see the image, its own compose file's bind mounts
            # (UNIT_MOUNTS above, and its config) and its systemd unit — nothing
            # else in this repo. **Add a bind mount to a compose file and you must
            # add it to that unit's row and keep its source off this list**;
            # `tests/test_deploy_wiring.py` fails if you don't.
            fomc_dates.txt|discretionary_blackout_dates.txt)
                INERT_CHANGES="${INERT_CHANGES}${changed} "
                continue
                ;;
            # ANCILLARY HOST UNITS. Not a strategy process: these are the nightly
            # DB snapshot, the NAS-pull staleness check, the EOD report and the
            # retired shadow timer. They run on the HOST, beside the containers,
            # and no container can read any of them — so a release that only moves
            # one must not tear down a session, the watchdog and its virtual stops.
            #
            # NAMED, never a `*.service|*.timer` glob: the strategy units ARE
            # visible and a glob would swallow them. Fail-closed means a unit
            # nobody listed here goes to VISIBLE, and a new ancillary unit has to
            # be added deliberately.
            #
            # INERT IS NOT THE SAME AS DEPLOYED. The deploy syncs the strategy
            # units and nothing else, so a change to one of these reaches
            # /etc/systemd only when a human copies it — which is why they are
            # collected into ANCILLARY_UNIT_CHANGES and shouted about in the plan
            # below rather than being folded silently into INERT_CHANGES.
            #
            # ONE LINE, no `\`-continuation: `test_nothing_compose_bind_mounts_is_
            # treated_as_inert_by_the_deploy` extracts these arms with a line-anchored
            # regex, so a wrapped arm is invisible to the check that no bind-mounted
            # file has been declared inert. Keep new names on this line.
            argus-snapshot.service|argus-snapshot.timer|argus-pull-check.service|argus-pull-check.timer|argus-eod.service|argus-eod.timer|argus-shadow.service|argus-shadow.timer)
                INERT_CHANGES="${INERT_CHANGES}${changed} "
                ANCILLARY_UNIT_CHANGES="${ANCILLARY_UNIT_CHANGES}${changed} "
                continue
                ;;
        esac
        CANDIDATE_CHANGES="${CANDIDATE_CHANGES}${changed} "
    done <<CHANGED_EOF
$(git diff --name-only "$PREV_TAG" "$VERSION")
CHANGED_EOF
else
    # Unknown prior state, or a deliberate re-deploy of the tag already checked
    # out — the documented way to recover a crashed container. Both restart every
    # unit that is running.
    UNKNOWN_PREV=1
fi

# unit_reads <row> <path>: can that unit's process read <path>? 0 = yes.
unit_reads() {
    local row="$1" path="$2" other f
    case "$path" in
        # A CONFIG FILE: only the one this unit's profile names. Another profile's
        # file, or another strategy's, is one its container never mounts — a cert
        # droplet cannot see a live-config edit, which is most of what the profile
        # split produces. This unit's own config falls through to visible.
        argus.*.toml)
            [ "$path" = "${U_CONFIG[$row]}" ]
            return
            ;;
    esac
    # ANOTHER STRATEGY'S OWN FILES: its unit, its compose file, its data mounts.
    for other in "${!UNIT_NAMES[@]}"; do
        [ "$other" = "$row" ] && continue
        for f in ${UNIT_FILES[$other]} ${UNIT_MOUNTS[$other]}; do
            [ "$path" = "$f" ] && return 1
        done
    done
    # Everything else: the image's inputs, this unit's own files, and anything
    # nobody classified. Fail-closed: unknown means visible.
    return 0
}

# NEED_BUILD is the release's, once: there is one image.
NEED_BUILD=0
if [ "$FORCE_BUILD" -eq 1 ] || [ -n "$CHANGED_BUILD_INPUTS" ]; then
    NEED_BUILD=1
fi
# NO IMAGE FOR THIS TAG ON THIS HOST: build, whatever the diff says. A re-run after a
# deploy that stopped between its checkout and its build sees PREV_TAG == VERSION,
# an empty diff, and would otherwise "deploy" the previous release's image under the
# new tag (the --force-build lesson, made automatic; found reviewing v0.6.0).
if [ "$NEED_BUILD" -eq 0 ] && ! sudo docker image inspect "argus:${VERSION}" >/dev/null 2>&1; then
    NEED_BUILD=1
    CHANGED_BUILD_INPUTS="(no argus:${VERSION} image on this host)"
fi

# Per installed unit: what it sees move, whether its unit file moved, whether it
# restarts, and the config bytes that SHOULD be loaded after.
U_VISIBLE=()
U_SYNC=()
U_RESTART=()
U_MOUNTED_CHANGES=()
U_SHA=()
for i in "${!UNIT_NAMES[@]}"; do
    U_VISIBLE[$i]=""
    U_SYNC[$i]=0
    U_RESTART[$i]=0
    U_MOUNTED_CHANGES[$i]=""
    U_SHA[$i]=""
    [ "${U_INSTALLED[$i]}" -eq 1 ] || continue
    CONFIG_FILE="${U_CONFIG[$i]}"

    # The profile's config file must EXIST in the tag being deployed, checked
    # against the tag rather than the working tree so --dry-run answers it too.
    # This is the check that catches a tag which renamed or dropped a profile file
    # — a failure that otherwise presents as a crash loop at restart, because
    # compose creates a DIRECTORY at the mount path when the host file is missing.
    if ! git ls-tree -r --name-only "$VERSION" -- "$CONFIG_FILE" | grep -qx "$CONFIG_FILE"; then
        echo "argus-deploy: ${VERSION} does not contain ${CONFIG_FILE}, and" >&2
        echo "${UNIT_NAMES[$i]} runs profile '${U_PROFILE[$i]}' (${UNIT_ENV_FILES[$i]})." >&2
        echo "argus-deploy: deploying it would bind-mount a missing file, which Docker" >&2
        echo "creates as a directory — the process refuses to start and the unit" >&2
        echo "crash-loops. Fix the profile or the tag; do not proceed." >&2
        exit 1
    fi

    if [ "$UNKNOWN_PREV" -eq 1 ]; then
        U_VISIBLE[$i]="(previous tag unknown or unchanged — restarting if running)"
        U_SYNC[$i]=1
        U_MOUNTED_CHANGES[$i]="(previous tag unknown)"
    else
        for changed in $CANDIDATE_CHANGES; do
            if unit_reads "$i" "$changed"; then
                U_VISIBLE[$i]="${U_VISIBLE[$i]}${changed} "
            fi
        done
        if [ -n "$(git diff --name-only "$PREV_TAG" "$VERSION" -- "${UNIT_FILES[$i]}")" ]; then
            U_SYNC[$i]=1
        fi
        # Of the config class, what THIS UNIT actually reads: its own profile's
        # file and its compose file's other mounts. A live-config edit landing on
        # a cert droplet is a real config change and a no-op there, and reporting
        # it as `config: YES` is how a deploy gets read as riskier than it is.
        # shellcheck disable=SC2086  # the mounts are a word list, by design
        U_MOUNTED_CHANGES[$i]="$(git diff --name-only "$PREV_TAG" "$VERSION" -- \
            "$CONFIG_FILE" ${UNIT_MOUNTS[$i]})"
    fi

    # The bytes that SHOULD be loaded after the restart, taken from the tag rather
    # than from the working tree — the working tree is only guaranteed to match
    # after the checkout, and --dry-run never checks anything out.
    U_SHA[$i]="$(git show "${VERSION}:${CONFIG_FILE}" | _sha12)"

    if [ -n "${U_VISIBLE[$i]}" ] || [ "$FORCE_RESTART" -eq 1 ]; then
        U_RESTART[$i]=1
    fi
    # A build or a unit sync always restarts, whatever the file classification
    # said: --force-build with no changed inputs produces a new image that only a
    # restart can pick up, and a re-synced unit only takes effect on one.
    if [ "$NEED_BUILD" -eq 1 ] || [ "${U_SYNC[$i]}" -eq 1 ]; then
        U_RESTART[$i]=1
    fi
done

echo "argus-deploy: plan"
echo "  from:  ${PREV_TAG:-<unknown>}"
echo "  to:    ${VERSION}"
if [ "$NEED_BUILD" -eq 1 ]; then
    echo "  build: YES"
    if [ -n "$CHANGED_BUILD_INPUTS" ]; then
        echo "         ${CHANGED_BUILD_INPUTS}"
    else
        echo "         (--force-build; inputs otherwise unchanged since ${PREV_TAG})"
    fi
else
    echo "  build: no (src/requirements.txt/pyproject.toml/Dockerfile unchanged" \
         "since ${PREV_TAG})"
fi
for i in "${!UNIT_NAMES[@]}"; do
    unit="${UNIT_NAMES[$i]}"
    if [ "${U_INSTALLED[$i]}" -ne 1 ]; then
        echo "  ${unit}: not installed on this host — skipped (its first install is"
        echo "         a one-time step by hand, in its runbook)"
        continue
    fi
    echo "  ${unit}:"
    echo "    profile: ${U_PROFILE[$i]} (${U_CONFIG[$i]}, sha ${U_SHA[$i]}) — from ${UNIT_ENV_FILES[$i]}"
    if [ -n "${U_MOUNTED_CHANGES[$i]}" ]; then
        echo "    config: YES"
        echo "           ${U_MOUNTED_CHANGES[$i]}"
    elif [ -n "$CHANGED_CONFIG_INPUTS" ]; then
        echo "    config: not for this unit — ${CHANGED_CONFIG_INPUTS} moved, but" \
             "${unit} (profile '${U_PROFILE[$i]}') does not mount it"
    else
        echo "    config: no (unchanged since ${PREV_TAG})"
    fi
    if [ "${U_SYNC[$i]}" -eq 1 ]; then
        echo "    unit:  ${UNIT_FILES[$i]} changed — will sudo cp + daemon-reload"
    fi
    if [ "${U_RESTART[$i]}" -eq 1 ]; then
        if [ "$FORCE_RESTART" -eq 1 ] && [ -z "${U_VISIBLE[$i]}" ]; then
            echo "    restart: YES if running (--restart; nothing it reads actually moved)"
        else
            echo "    restart: YES if running — a STOPPED unit stays stopped"
        fi
    else
        echo "    restart: no — nothing this unit's process can see moved."
        echo "           (a restart tears down a session or a capture. Pass"
        echo "            --restart to force one.)"
    fi
done
if [ -n "$INERT_CHANGES" ]; then
    echo "  inert to every unit: ${INERT_CHANGES}"
fi
# THE ONE THING THIS DEPLOY WILL NOT DO FOR YOU. These units run on the host, not
# in a container, so they are correctly inert for the restart decision — but the
# deploy syncs the strategy units and nothing else, so a change here lands in the
# checkout and NOWHERE ELSE. Left unsaid, a fix to the nightly snapshot or to the
# backup-pull check would deploy green and change nothing: the silent-reattach
# failure the version read-back exists to catch, one unit over and with no
# read-back available, because a timer reports no version.
if [ -n "$ANCILLARY_UNIT_CHANGES" ]; then
    echo "  ancillary units CHANGED — the deploy does NOT install these:"
    echo "         ${ANCILLARY_UNIT_CHANGES}"
    echo "         run by hand AFTER the deploy, or the change is a no-op:"
    for _u in $ANCILLARY_UNIT_CHANGES; do
        echo "           sudo cp ${REPO_DIR}/${_u} /etc/systemd/system/"
    done
    echo "           sudo systemctl daemon-reload"
    echo "         then re-enable any .timer among them:"
    echo "           sudo systemctl enable --now <name>.timer"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    # THE CONFIG DIFF IS THE REVIEW. On a config-only release that diff *is* the
    # release — there is no source change to read instead — and this is the last
    # moment before risk rails, staleness bounds and COA timing change under a
    # restart. Shown for each installed unit's OWN profile only: the other
    # profiles' files are not mounted here and diffing them would bury the ones
    # that are.
    if [ -n "$PREV_TAG" ]; then
        for i in "${!UNIT_NAMES[@]}"; do
            [ "${U_INSTALLED[$i]}" -eq 1 ] || continue
            echo ""
            echo "argus-deploy: ${U_CONFIG[$i]}: ${PREV_TAG} -> ${VERSION}"
            if git diff --quiet "$PREV_TAG" "$VERSION" -- "${U_CONFIG[$i]}"; then
                echo "  (no change)"
            else
                git --no-pager diff "$PREV_TAG" "$VERSION" -- "${U_CONFIG[$i]}"
            fi
        done
        echo ""
    fi
    echo "argus-deploy: --dry-run — stopping here. Nothing was checked out," \
         "built, tagged, or restarted."
    exit 0
fi

echo "argus-deploy: checking out ${VERSION} ..."
git checkout "$VERSION"

# VERIFY BEFORE BUILDING — the step that would have caught the 2026-08-05
# no-op-checkout incident (a detached-HEAD `git pull` silently no-ops; nothing
# else here would have noticed).
ACTUAL="$(git describe --tags)"
if [ "$ACTUAL" != "$VERSION" ]; then
    echo "argus-deploy: checkout landed on '${ACTUAL}', not '${VERSION}'. Aborting" \
         "before build — deploying the wrong code silently is worse than stopping." >&2
    exit 1
fi
DIRTY_AFTER="$(git status --short)"
if [ -n "$DIRTY_AFTER" ]; then
    echo "argus-deploy: tree is dirty immediately after checkout — investigate" \
         "before proceeding:" >&2
    echo "$DIRTY_AFTER" >&2
    exit 1
fi
echo "argus-deploy: on ${ACTUAL}, tree clean. Proceeding."

# HAND OVER TO THE VERSION OF THIS SCRIPT THAT SHIPPED WITH THE TAG, if the
# checkout changed it. Everything from here on — the build invocation, the unit
# sync, the read-backs — belongs to the release being deployed, and the release
# is the authority on how its own steps are run.
#
# Naturally single-shot: the re-exec'd copy computes SELF_SHA_BEFORE from the file
# it already is, and its own checkout is a no-op, so it cannot differ again. The
# explicit guard is belt-and-braces against a checkout filter that rewrites on
# every pass, which would otherwise be an unbounded loop.
SELF_SHA_AFTER="$(_sha12 < "$SELF")"
if [ "$SELF_SHA_AFTER" != "$SELF_SHA_BEFORE" ]; then
    if [ "${ARGUS_DEPLOY_REEXECED:-0}" = "1" ]; then
        echo "argus-deploy: the deploy script changed AGAIN after re-exec" >&2
        echo "(${SELF_SHA_BEFORE} -> ${SELF_SHA_AFTER}). Refusing to loop; run it" >&2
        echo "again by hand now that the tree is on ${ACTUAL}." >&2
        exit 1
    fi
    echo "argus-deploy: ${VERSION} ships a different deploy script" \
         "(${SELF_SHA_BEFORE} -> ${SELF_SHA_AFTER})."
    echo "argus-deploy: handing over to it — the release decides how its own" \
         "steps run."
    exec env ARGUS_DEPLOY_REEXECED=1 ARGUS_DEPLOY_PREV_TAG="$PREV_TAG" \
        bash "$SELF" "$@"
fi

# The mount reads the WORKING TREE, not the git object, so the working tree is
# what the expectation has to be taken from. Recomputed here and compared against
# the tag-derived value above: they can only disagree through a checkout filter or
# a file that changed under us between the two reads, and either one would make
# the read-back below assert against a number nothing will ever produce.
for i in "${!UNIT_NAMES[@]}"; do
    [ "${U_INSTALLED[$i]}" -eq 1 ] || continue
    WORKTREE_CONFIG_SHA="$(_sha12 < "${U_CONFIG[$i]}")"
    if [ "$WORKTREE_CONFIG_SHA" != "${U_SHA[$i]}" ]; then
        echo "argus-deploy: ${U_CONFIG[$i]} on disk hashes ${WORKTREE_CONFIG_SHA}, but" >&2
        echo "${VERSION}'s object hashes ${U_SHA[$i]}. The file that will be" >&2
        echo "mounted is not the file the tag carries — investigate before deploying." >&2
        exit 1
    fi
done

if [ "$NEED_BUILD" -eq 1 ]; then
    echo "argus-deploy: building ${VERSION} ..."
    # THE PLATFORM'S IMAGE, built ONCE, by docker itself rather than through one
    # strategy's compose file: every strategy runs this image. (Through v0.5.x
    # this was `docker compose build`, which names the image from the compose
    # file's `argus:${ARGUS_IMAGE_TAG:-local}` — so a rollback pin left in ./.env
    # would have stamped the new build with the pinned tag, and the `docker tag
    # argus:local` after it would have tagged a stale image as this release.)
    #
    # STAMP THE BUILD. `ARGUS_VERSION` is baked into `argus/_version.py` at image
    # build time and is what `GET /status` and the first line of the startup log
    # report; omit it and the image builds fine, restarts fine, logs fine, and
    # reports `version: "unknown"` forever — a clean green deploy that silently
    # defeats the whole point of the field.
    #
    # `$ACTUAL`, not a fresh `git describe --tags --always --dirty`. That is what
    # the runbook's MANUAL command uses because a hand-run build has verified
    # nothing; this script has already asserted, above, that the checkout landed
    # exactly on ${VERSION} and that the tree is clean. Re-deriving the string here
    # would introduce a second read that could disagree with the one that was
    # validated — and stamping a value the script did not check is the same class
    # of gap as not stamping at all.
    #
    # Tag == the git tag, always, never a date, beside `argus:local` (what each
    # compose file runs unless ./.env pins ARGUS_IMAGE_TAG). Every build overwrites
    # argus:local in place, so the version tag is the one thing standing between
    # "rollback" and "there is nothing to roll back to." (There was nothing to roll
    # back to, on every release before 2026-08-19: `docker images argus` returned
    # one row.)
    sudo docker build --build-arg "ARGUS_VERSION=${ACTUAL}" \
        -t argus:local -t "argus:${VERSION}" -f Dockerfile .
    echo "argus-deploy: tagged argus:local and argus:${VERSION}"
elif [ -n "$CHANGED_CONFIG_INPUTS" ]; then
    echo "argus-deploy: skipping build (config-only release)"
else
    # NAME THE CLASS. v0.3.19 was a scripts-only release and this line called it
    # "config-only" — the wrong class, on the release whose whole content was the
    # deploy script. An operator reading a deploy should never be told the wrong
    # thing about what just happened.
    echo "argus-deploy: skipping build (nothing in src/, deps, or the Dockerfile" \
         "moved)"
fi

SYNCED=0
for i in "${!UNIT_NAMES[@]}"; do
    [ "${U_INSTALLED[$i]}" -eq 1 ] && [ "${U_SYNC[$i]}" -eq 1 ] || continue
    echo "argus-deploy: syncing ${UNIT_NAMES[$i]} ..."
    sudo cp "${UNIT_FILES[$i]}" "${SYSTEMD_DIR}/${UNIT_NAMES[$i]}"
    SYNCED=1
done
if [ "$SYNCED" -eq 1 ]; then
    sudo systemctl daemon-reload
fi

# --- RESTART: ONLY WHAT IS RUNNING ------------------------------------------------
#
# `try-restart`, never `restart` or `start`: systemd restarts the unit only if it is
# running, and does NOTHING to a stopped, disabled or failed one. That is the whole
# safety property of this block, and it lives in the verb rather than in the
# is-active check beside it (which only decides what to report and read back), so
# no race between the two can start a unit. A strategy is started by a person, on
# purpose — never as a side effect of deploying code: on the droplet today the SPX
# book's unit is stopped and disabled, with its kill switch engaged (2026-10-01).
#
# RESTART_AT is taken BEFORE any restart, and it is load-bearing for the version
# read-back below. The whole failure being checked for is "the new process did not
# come up", and in that case the journal still contains the PREVIOUS process's
# `argus build` line — which would read as a success. Anchoring the query to this
# instant means the check can only ever see the boot it just caused.
EXIT_CODE=0
RESTART_AT="$(date '+%Y-%m-%d %H:%M:%S')"
U_RESTARTED=()
U_RUNNING=()
ANY_RESTARTED=0
for i in "${!UNIT_NAMES[@]}"; do
    U_RESTARTED[$i]=0
    U_RUNNING[$i]=0
    [ "${U_INSTALLED[$i]}" -eq 1 ] || continue
    unit="${UNIT_NAMES[$i]}"
    if sudo systemctl is-active --quiet "$unit"; then
        U_RUNNING[$i]=1
    fi
    if [ "${U_RESTART[$i]}" -ne 1 ]; then
        if [ "${U_RUNNING[$i]}" -eq 1 ]; then
            echo "argus-deploy: NOT restarting ${unit} — nothing this release moves is"
            echo "read by its process. THE CHECKOUT IS THE DEPLOY. (A restart is not"
            echo "free; re-run with --restart if you meant to bounce it anyway.)"
        fi
        continue
    fi
    if [ "${U_RUNNING[$i]}" -eq 1 ]; then
        echo "argus-deploy: restarting ${unit} ..."
        sudo systemctl try-restart "$unit"
        U_RESTARTED[$i]=1
        ANY_RESTARTED=1
    else
        echo "argus-deploy: ${unit} is NOT RUNNING (stopped, disabled or failed): LEFT"
        echo "AS IT IS. The deploy never starts a strategy. The checkout and the image"
        echo "are in place for whenever someone starts it on purpose."
    fi
done

# Give each restarted process a few seconds to either come up clean or fall over,
# then read the two signals that actually distinguish those cases. `systemctl
# is-active` alone does NOT: `restart: unless-stopped` + `Restart=always` keep the
# COMPOSE PROCESS "active" even while the PROCESS INSIDE is crash-looping on a
# ConfigError — exactly the 2026-08-14 incident (34 restarts, "active (running)"
# the entire time, no /status, no /kill). So this also greps the journal for the
# specific strings that postmortem named.
if [ "$ANY_RESTARTED" -eq 1 ]; then
    echo "argus-deploy: waiting for the restarted process(es) to report in ..."
    sleep "$SETTLE_S"
fi

RED_FLAGS='ConfigError|missing required config key|missing or malformed|refusing to start|reconciliation_mismatch|Traceback'
GOOD_SIGNS='argus started in|broker reconciliation clean'

# read_back <row>: what came up for one RUNNING unit, read from its own journal.
read_back() {
    local row="$1"
    local unit="${UNIT_NAMES[$row]}"
    local restarted="${U_RESTARTED[$row]}"
    local PROFILE="${U_PROFILE[$row]}"
    local CONFIG_FILE="${U_CONFIG[$row]}"
    local EXPECTED_CONFIG_SHA="${U_SHA[$row]}"
    local VERSION_LOG RUNNING_VERSION RUNNING_CONFIG_LINE
    local RUNNING_CONFIG RUNNING_CONFIG_SHA

    echo ""
    echo "argus-deploy: ${unit}:"
    # WHAT ACTUALLY CAME UP, read back from the process rather than assumed. This
    # is the one check that catches the hazard the runbook can only warn about: a
    # unit has no build step, so a restart with no fresh build silently reattaches
    # to whichever image was last built — no error, no red flag, just the previous
    # release running under a new git tag. Everything above would still pass.
    #
    # Bounded by `--since $RESTART_AT`, never a line count: `argus build` is the
    # FIRST line of startup, and a mid-day restart whose session comes straight up
    # can push it out of any fixed window and produce a false "no version line"
    # warning. The time bound is both wider (everything this boot logged) and
    # narrower (nothing the last one did).
    #
    # WITH NO RESTART there is no boot to anchor to, and `--since` a timestamp that
    # never happened would find nothing and warn falsely. The query becomes "the
    # last startup line, whenever it was" — `--grep` so a long-running process's
    # volume cannot push it out of any window — and the question it answers weakens
    # accordingly: not "did the new process come up right?" but "is what has been
    # running all along still what this tag expects?". That is worth asking. It is
    # how a droplet hand-edited since its last restart, or a config release that
    # was checked out and never restarted, gets caught.
    if [ "$restarted" -eq 1 ]; then
        VERSION_LOG="$(sudo journalctl -u "$unit" --no-pager --since "$RESTART_AT" || true)"
        # THE BOOT THIS DEPLOY CAUSED, judged on its own lines. (Through v0.5.x this read
        # the last 60 lines, which for a process that logs a handful of lines a boot
        # is mostly the PREVIOUS boot: an old Traceback failed the deploy, an old
        # startup line passed it.) Here-strings, not `echo | grep -q`: under pipefail
        # a match can kill the echo with SIGPIPE and read as no match.
        if grep -qE "$RED_FLAGS" <<<"$VERSION_LOG"; then
            echo "" >&2
            echo "argus-deploy: !! ${unit}: DEPLOYED BUT THE JOURNAL SHOWS TROUBLE !!" >&2
            echo "argus-deploy: matching line(s):" >&2
            grep -E "$RED_FLAGS" <<<"$VERSION_LOG" >&2
            echo "" >&2
            echo "argus-deploy: this can be 'active (running)' the whole time it is" >&2
            echo "crash-looping (2026-08-14) — do not trust systemctl status alone." >&2
            echo "argus-deploy: check it by hand, and roll back if needed with:" >&2
            echo "argus-deploy:   $0 ${PREV_TAG:-<previous known-good tag>}" >&2
            EXIT_CODE=2
        elif grep -qE "$GOOD_SIGNS" <<<"$VERSION_LOG"; then
            echo "argus-deploy: journal looks clean:"
            grep -E "$GOOD_SIGNS" <<<"$VERSION_LOG" | tail -n 5
        else
            echo "argus-deploy: no red flags, but no confirmation line either yet —" \
                 "check journalctl -u ${unit} -f by hand."
        fi
        # (Nothing was restarted otherwise, so the journal describes a process this
        # deploy did not touch: judging it would report a pre-existing condition as
        # this release's failure.)
    else
        VERSION_LOG="$(sudo journalctl -u "$unit" --no-pager -g 'argus build' 2>/dev/null | tail -n 5 || true)"
        if [ -z "$VERSION_LOG" ]; then
            # `--grep` needs systemd 237+; fall back to a bounded tail rather than
            # reading the whole journal.
            VERSION_LOG="$(sudo journalctl -u "$unit" --no-pager -n 5000 || true)"
        fi
    fi
    RUNNING_VERSION="$(echo "$VERSION_LOG" | grep -oE 'argus build [^[:space:]]+' \
                       | tail -n 1 | awk '{print $3}' || true)"
    if [ -n "$RUNNING_VERSION" ]; then
        if [ "$restarted" -eq 1 ]; then
            echo "argus-deploy: running build reports: ${RUNNING_VERSION}"
        else
            echo "argus-deploy: still running (not restarted): ${RUNNING_VERSION}"
        fi
    fi
    # Only meaningful when we BUILT and restarted. On a config-only release the
    # image is deliberately untouched, so the running version legitimately still
    # names the previous tag, and demanding a match would fail every correct
    # config deploy.
    if [ "$NEED_BUILD" -eq 1 ] && [ "$restarted" -eq 1 ]; then
        if [ -z "$RUNNING_VERSION" ]; then
            echo "argus-deploy: WARNING — no 'argus build' line in the journal. Either" >&2
            echo "the process has not logged yet, or this image predates the version" >&2
            echo "stamp. Check its startup line by hand." >&2
        elif [ "$RUNNING_VERSION" = "unknown" ]; then
            echo "" >&2
            echo "argus-deploy: !! THE RUNNING IMAGE IS UNSTAMPED (version: unknown) !!" >&2
            echo "argus-deploy: the build arg did not reach the image, so nothing at" >&2
            echo "runtime can say what is deployed. Rebuild with --force-build." >&2
            EXIT_CODE=2
        elif [ "$RUNNING_VERSION" != "$ACTUAL" ]; then
            echo "" >&2
            echo "argus-deploy: !! RUNNING ${RUNNING_VERSION}, DEPLOYED ${ACTUAL} !!" >&2
            echo "argus-deploy: the restart reattached to an older image — the build" >&2
            echo "did not take, or the container did not actually restart. This is the" >&2
            echo "failure the version field exists to make visible; do not ignore it." >&2
            EXIT_CODE=2
        fi
    fi

    # WHICH CONFIG BYTES CAME UP — the version stamp's other half, and read off the
    # SAME startup line so one anchor answers both. Unlike the version read-back
    # this runs on EVERY release class: the image is deliberately untouched on a
    # config-only release, which is exactly why the version cannot speak for it and
    # why that class verified nothing at all before this existed.
    #
    # `argus build <version> config <profile>/<filename> @<sha>` (app.py for the SPX
    # book: one line, every strategy). The profile and
    # filename are compared too, not just the sha: a mismatch there is the mount
    # path drifting, or a stale ARGUS_CONFIG in ./.env winning over the profile —
    # both of which a sha comparison alone would report as an unexplained hash.
    RUNNING_CONFIG_LINE="$(echo "$VERSION_LOG" \
        | grep -oE 'argus build [^[:space:]]+ config [^[:space:]]+ @[^[:space:]]+' \
        | tail -n 1 || true)"
    if [ -z "$RUNNING_CONFIG_LINE" ]; then
        echo "argus-deploy: WARNING — the startup line carries no config fingerprint." >&2
        echo "Either the process has not logged yet, or this image predates v0.3.17." >&2
        echo "On a rollback to an older image that is expected; otherwise check" >&2
        echo "its startup line by hand." >&2
        return 0
    fi
    RUNNING_CONFIG="$(echo "$RUNNING_CONFIG_LINE" | awk '{print $5}')"
    RUNNING_CONFIG_SHA="$(echo "$RUNNING_CONFIG_LINE" | awk '{print $6}' | tr -d '@')"
    if [ "$restarted" -eq 1 ]; then
        echo "argus-deploy: running config reports: ${RUNNING_CONFIG} @${RUNNING_CONFIG_SHA}"
    else
        echo "argus-deploy: config still loaded (not restarted): ${RUNNING_CONFIG} @${RUNNING_CONFIG_SHA}"
    fi
    if [ "$RUNNING_CONFIG" != "${PROFILE}/${CONFIG_FILE}" ]; then
        echo "" >&2
        echo "argus-deploy: !! RUNNING CONFIG ${RUNNING_CONFIG}, EXPECTED ${PROFILE}/${CONFIG_FILE} !!" >&2
        echo "argus-deploy: the process did not load this host's profile. Check that" >&2
        echo "its compose file still mounts its config by \${ARGUS_ENV} and that" >&2
        echo "./.env carries no leftover ARGUS_CONFIG line (an explicit path WINS" >&2
        echo "over the profile)." >&2
        EXIT_CODE=2
    elif [ "$RUNNING_CONFIG_SHA" != "$EXPECTED_CONFIG_SHA" ]; then
        echo "" >&2
        echo "argus-deploy: !! RUNNING CONFIG @${RUNNING_CONFIG_SHA}, DEPLOYED @${EXPECTED_CONFIG_SHA} !!" >&2
        echo "argus-deploy: the bytes that came up are not the bytes ${VERSION}" >&2
        echo "carries. The restart may have beaten the checkout, the mount may be" >&2
        echo "stale, or the file was edited after the checkout. On a config-only" >&2
        echo "release this is the ONLY assertion there is — do not wave it through." >&2
        if [ "$restarted" -eq 0 ]; then
            echo "argus-deploy: NOTE — nothing was restarted, and this tag does not" >&2
            echo "change ${CONFIG_FILE}. So the mismatch predates this deploy: the" >&2
            echo "process has been running a config that is not the one on disk." >&2
        fi
        EXIT_CODE=2
    fi
}

for i in "${!UNIT_NAMES[@]}"; do
    [ "${U_INSTALLED[$i]}" -eq 1 ] || continue
    if [ "${U_RUNNING[$i]}" -eq 1 ]; then
        read_back "$i"
    else
        echo ""
        echo "argus-deploy: ${UNIT_NAMES[$i]}: not running — nothing to read back."
    fi
done

echo ""
echo "argus-deploy: image tags on this host:"
sudo docker images argus
echo ""
echo "argus-deploy: done. Every unit restarted above was read back from its own"
echo "journal. The SPX book, when running, also answers on /status:"
echo '  curl -fsS -H "Authorization: Bearer $ARGUS_KILL_TOKEN" http://<tailscale-ip>:8443/status | jq "{version, config}"'

exit "$EXIT_CODE"
