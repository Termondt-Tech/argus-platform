# Argus — 0DTE SPX execution system.
#
# Packaging only. Nothing in here changes runtime behaviour: no logic, no
# safety rails, no timing. The container runs the same `python -m argus`
# supervisor the host runs, with the same config file and the same env-injected
# credentials.
#
# Two things this image exists to guarantee:
#   1. The interpreter and EVERY transitive dependency are pinned by digest /
#      hash, so shadow mode and any future live deploy run identical code.
#   2. An image tag is a complete, self-contained rollback unit.

# Pinned by digest, not just tag: `python:3.12.13-slim-bookworm` is mutable and
# will silently become 3.12.14. Multi-arch manifest, so this resolves correctly
# on both an arm64 dev Mac and an amd64 droplet.
ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

# ---------------------------------------------------------------------------
# Stage 1: builder — resolve nothing, verify everything, leave no toolchain
# behind. Everything lands in a self-contained venv that stage 2 copies whole.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies first, on their own layer: they change far less often than the
# source, so an ordinary code change reuses this layer.
#
# --require-hashes is the point of the exercise. Every dependency must appear in
# requirements.txt with a matching hash or the BUILD FAILS. There is no path
# where pip quietly resolves something newer.
COPY requirements.txt ./
RUN pip install --require-hashes --no-deps -r requirements.txt

# Now the application itself. --no-deps because requirements.txt is already the
# complete, verified closure; letting pip re-resolve from pyproject.toml here
# would defeat the lock.
COPY pyproject.toml ./
COPY src/ ./src/

# STAMP THE BUILD before installing it. `git describe --tags --always --dirty`,
# computed by the caller (the runbook's build step) and passed in — the runtime image
# contains no `.git` and no git binary, so nothing inside a running container could
# derive this, and an env var set at `docker compose up` time was rejected as one
# more thing to forget. Unstamped is `unknown`, which is a real answer, so this
# NEVER fails a build for being absent.
#
# The value is sanitized to a safe character set before it is written into a Python
# source line — a build arg is caller-controlled text and this generates code — and
# truncated, so a pathological tag cannot produce a broken module. The `grep` is the
# build-time assertion the rest of this file's checks follow: a substitution that
# silently did not land would ship a confident `unknown` on a release build.
ARG ARGUS_VERSION=unknown
RUN VERSION="$(printf '%s' "$ARGUS_VERSION" | tr -cd 'A-Za-z0-9._+/-' | cut -c1-64)" \
 && sed -i "s|^BUILD_VERSION = .*|BUILD_VERSION = \"${VERSION:-unknown}\"|" \
        src/argus/_version.py \
 && grep -q '^BUILD_VERSION = "' src/argus/_version.py \
 && echo "argus build version: $(sed -n 's|^BUILD_VERSION = "\(.*\)"$|\1|p' src/argus/_version.py)"

RUN pip install --no-deps .

# ---------------------------------------------------------------------------
# Stage 2: runtime — venv only. No pip cache, no build context, no source tree.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# PYTHONUNBUFFERED is load-bearing, not hygiene: the shadow tooling reports
# through print() and the supervisor through logging. Without it stdout is
# block-buffered when not a TTY and `docker logs` / journalctl show NOTHING for
# minutes at a time — you would be flying blind while believing the process
# had hung.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# NO `ARGUS_CONFIG` HERE, and never again. It used to default to
# /etc/argus/argus.toml, which is an image-level default config path — the exact
# shape v0.3.17 deleted from the code, one layer down — and it WINS over the
# profile (`config.resolve_config_path`: an explicit path beats `ARGUS_ENV`). So
# with the profile model in place it would silently disarm the whole mechanism:
# the profile would name a file nothing loads, and /status would report a profile
# the loader ignored. The container gets `ARGUS_ENV` + `ARGUS_CONFIG_DIR` from
# docker-compose.yml instead.

# Dedicated unprivileged user. Fixed uid/gid so bind-mounted state has stable
# ownership across image rebuilds and across hosts.
RUN groupadd --system --gid 10001 argus \
 && useradd --system --uid 10001 --gid argus --no-create-home --shell /usr/sbin/nologin argus

COPY --from=builder /opt/venv /opt/venv

# NO CONFIG IS BAKED IN, deliberately — this used to `COPY argus.toml
# /etc/argus/argus.toml` as a "default so the image runs standalone", and that is
# a default config by another name. Two reasons it had to go with the profile
# rename, and the second is the sharper one:
#
#   1. It is an image-level default, and defaults are how the wrong database gets
#      opened (2026-07-21). The image must not carry an answer to "which config?".
#   2. It is the ONE failure the config fingerprint cannot see. A baked copy sits
#      at the same path the bind-mount targets, so a drifted or missing mount
#      would fall back to it silently — and because both come from the same git
#      checkout, the sha would MATCH and the deploy's read-back would pass. With
#      nothing baked, a drifted mount is a hard `ConfigError` on every profile.
#
# The config arrives only through docker-compose.yml's bind mount, which is also
# what keeps it retunable without a rebuild and versioned by git on the droplet
# rather than by the image tag.

# /data is the ONLY writable path and the mount point for the state volume
# (SQLite audit log + WAL sidecars). Created with the right ownership HERE so
# that Docker propagates it when it initialises a fresh named volume — otherwise
# the volume comes up root-owned and the unprivileged process cannot write.
RUN mkdir -p /data && chown argus:argus /data
VOLUME ["/data"]

# WORKDIR is /data on purpose. `audit.db_path` in the config is a RELATIVE path
# ("argus.db"), resolved against the working directory, so this places the
# database on the volume without editing the config the host also uses.
# Consequence worth knowing: anything run inside this container from a different
# working directory writes to a DIFFERENT, empty database rather than erroring.
WORKDIR /data

USER argus

# Fail the deploy at build time rather than at 09:30 ET if the package or its
# exchange-timezone data did not make it into the image.
RUN python -c "import argus, argus.clock; assert argus.clock.et_now().tzinfo is not None"

# Report the stamped version into the build log. NOT an assertion: `unknown` is a
# legitimate result for an ad hoc build, and refusing it here would turn a monitoring
# label into a rail. What IS asserted is that the module imports and answers.
RUN python -c "from argus._version import build_version; print('argus build:', build_version())"

# The always-on supervisor — the same entry point as a direct host run.
# Compose overrides `command:` to run the time-boxed shadow tooling instead.
CMD ["python", "-m", "argus"]
