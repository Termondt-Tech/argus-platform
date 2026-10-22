#!/usr/bin/env bash
#
# Consistent, verified snapshot of the Argus audit DB to a host path off the
# Docker volume — so the hash chain exists somewhere other than argus-state, and
# your NAS has something safe to pull. This is the interim durability story until
# Litestream lands (see README §4 / CLAUDE.md).
#
# WHY NOT `cp argus.db`: the DB is WAL-mode. A filesystem copy taken mid-write
# captures a torn, inconsistent file (committed frames still in the -wal, missed
# by the copy). This uses SQLite's ONLINE BACKUP API instead, which produces a
# consistent snapshot even while a session is writing, and it opens the source
# READ-ONLY so it never contends for the single-writer lock — safe to run during
# a live shadow session.
#
# The snapshot is written to a HOST directory (default /path/to/backups),
# deliberately NOT inside the argus-state volume: a `docker compose down -v`
# destroys the volume, and backups living inside it would die with it.
#
# Run it via argus-snapshot.timer (nightly after the close), or by hand:
#   sudo /opt/argus/scripts/argus-snapshot.sh
#
# Overridable via env:
#   ARGUS_COMPOSE_FILE  (default /opt/argus/docker-compose.yml)
#   ARGUS_BACKUP_DIR    (default /path/to/backups)
#   ARGUS_BACKUP_KEEP   (default 14 — snapshots to retain)

set -euo pipefail

COMPOSE_FILE="${ARGUS_COMPOSE_FILE:-/opt/argus/docker-compose.yml}"
BACKUP_DIR="${ARGUS_BACKUP_DIR:-/path/to/backups}"
KEEP="${ARGUS_BACKUP_KEEP:-14}"

# THE PROFILE MUST BE IN THE ENVIRONMENT. Since v0.3.18 compose interpolates
# `${ARGUS_ENV:?}` into the container env AND into the config bind mount, so
# `docker compose run` below REFUSES without it — on `run`, not only `up`.
#
# Under systemd it arrives via argus-snapshot.service's `EnvironmentFile`. A
# hand-run gets nothing (sudo scrubs the environment), so refuse HERE with the
# command to use rather than letting compose fail with an interpolation error
# three lines into a backup job whose whole point is to be trustworthy.
#
# Deliberately NOT defaulted and deliberately NOT parsed out of host.env here:
# a default is the failure `${ARGUS_ENV:?}` exists to prevent, and a third reader
# of that file is a third chance for two of them to disagree.
if [ -z "${ARGUS_ENV:-}" ]; then
    echo "argus-snapshot: ARGUS_ENV is unset, so \`docker compose\` will refuse." >&2
    echo "argus-snapshot: under systemd it comes from /etc/argus/host.env via" >&2
    echo "argus-snapshot: argus-snapshot.service's EnvironmentFile. By hand:" >&2
    echo "argus-snapshot:   PROFILE=\"\$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)\"" >&2
    echo "argus-snapshot:   sudo ARGUS_ENV=\"\$PROFILE\" /opt/argus/scripts/argus-snapshot.sh" >&2
    exit 1
fi
STAMP="$(date +%Y%m%dT%H%M%S)"
DEST="argus-${STAMP}.db"

# The container writes as uid 10001 (the image's non-root `argus` user), so the
# backup dir must exist and be writable by it. Idempotent; needs root (the
# systemd unit runs as root).
mkdir -p "$BACKUP_DIR"
chown 10001:10001 "$BACKUP_DIR"

echo "argus-snapshot: writing ${BACKUP_DIR}/${DEST}"

# Backup + verify in one shot, inside the container (same SQLite that writes the
# DB, and the argus package for verify_chain). The bind mount lands the snapshot
# on the host path. A failed verify raises -> non-zero exit -> set -e aborts ->
# the systemd unit is marked failed. A backup you have not verified is a guess.
docker compose -f "$COMPOSE_FILE" run --rm -T \
    -v "${BACKUP_DIR}:/backup" argus python - "$DEST" <<'PY'
import sqlite3, sys
from pathlib import Path

from argus.audit.log import AuditLog
from argus.config import Config

# WHICH DATABASE. Resolved through the SAME loader `python -m argus` uses, with
# no argument, so this backup finds its source exactly the way the process being
# backed up finds its destination: `ARGUS_ENV` names the file, `ARGUS_CONFIG_DIR`
# says where, `audit.db_path` says which DB, and the Dockerfile's `WORKDIR /data`
# is what makes that relative path land in the state volume.
#
# IT WAS A LITERAL — `file:/data/argus.db?mode=ro` — and that was a live-cutover
# blocker, because `argus.live.toml` carries `db_path = "argus-live.db"` (a
# DIFFERENT FILE, deliberately, so live can never continue the cert hash chain).
# Post-cutover this would have copied the frozen CERT database every night while
# every observable downstream stayed green: the copy succeeds, `verify_chain`
# passes on an intact-but-finished chain, a fresh argus-<stamp>.db lands, and
# `argus-pull-check.sh` reports `ok` on its freshness test. The live audit log —
# the only durable record of real orders and real fills — would have had no
# off-volume copy, and nothing would have said so.
#
# NEVER REPLACE THIS WITH A PATH BUILT HERE, and in particular never with a
# `sandbox -> argus.db / live -> argus-live.db` case: that is a SECOND authority
# for which database this host writes, and the two disagree the first time a
# profile is added. One loader, one answer.
# -> docs/live-cutover-runbook.md section 2
cfg = Config.load()
src_path = Path(cfg.audit.db_path).resolve()
if not src_path.exists():
    # LOUD, not skipped. The database the loaded profile names is absent, which
    # means the profile changed and the process has not come up under it yet (the
    # cutover window), or db_path moved under a running process. Either way a
    # backup of something else is the wrong answer.
    raise SystemExit(
        f"argus-snapshot: {src_path} does not exist. That is the database "
        f"{cfg.source.filename if cfg.source else 'the loaded config'} names "
        f"(audit.db_path = {cfg.audit.db_path!r}). Refusing to back up anything "
        f"else — check that the process has started under this profile."
    )

# Say which database, every run. A number nobody prints is a number nobody
# notices, and the whole failure above was invisible precisely because no
# surface named its subject.
print(
    f"argus-snapshot: source {src_path} "
    f"(profile {cfg.source.profile if cfg.source else '-'}, "
    f"environment {cfg.environment})"
)

dest = f"/backup/{sys.argv[1]}"
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)   # read-only, no lock
out = sqlite3.connect(dest)
try:
    with out:
        src.backup(out)
    # `backup` copies the source's WAL journal mode into the destination, which
    # leaves -wal/-shm sidecars. Collapse to a single self-contained file so the
    # NAS can pull just the .db and have a complete, restorable snapshot.
    out.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    out.execute("PRAGMA journal_mode=DELETE")
finally:
    src.close(); out.close()

chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
chk.row_factory = sqlite3.Row
AuditLog(chk, tier2_queue_maxsize=1, tier2_queue_alert_depth=1).verify_chain()
n = chk.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
chk.close()
print(f"argus-snapshot: verified {dest} ({n} records, chain intact)")
PY

# World-readable so the NAS can pull as an ordinary (non-root, non-docker) user.
# The chain holds signal data only — never credentials — so this is not a secret.
chmod 0644 "${BACKUP_DIR}/${DEST}"

# Rotate: keep the newest $KEEP FILES, delete the rest.
#
# RETENTION IS NOT CHEAP, and the comment here used to say it was — it had taken
# README's "~7 MB per full session" (a GROWTH RATE) and applied it to a snapshot,
# which is a complete copy of the whole DB. Measured 2026-09-06: **490 MB per
# snapshot** at 688,265 records, so `KEEP=14` is ~6.9 GB standing on the droplet's
# disk today. It tracks the DB rather than the day's activity, so at README's
# ~1.8 GB/year it is ~32 GB a year from now. Check `df -h` before raising KEEP,
# and note that go-live-findings still lists droplet disk headroom as open.
#
# **$KEEP COUNTS FILES, NOT DAYS.** Nothing here is keyed to the trading date, so N
# runs in one day evict N days off the back — three hand-runs while verifying the
# v0.3.20 deploy rotated out three. That is safe only because the NAS pulls every
# 6h and had already collected them; it is NOT safe on a day the pull is broken,
# which is exactly when someone is most likely to be running this by hand. If you
# are debugging, run it ONCE and read the output rather than re-running it.
#
# **AND IT COUNTS FILES ACROSS PROFILES.** The name carries a timestamp and
# nothing else, so after a profile change this directory transiently holds
# snapshots of two DIFFERENT databases under identical-looking names, and $KEEP
# runs of the new one rotate the old one's out. That is the documented model
# working — the droplet is a staging window and the NAS is the archive (runbook
# section 13) — but it means a restore cannot tell the two apart from the
# filename. The journal line the backup now prints names the source; and the
# live-cutover procedure takes ONE final cert snapshot and moves it OUT of this
# glob, which is the only copy of that distinction that survives on this box.
# -> docs/live-cutover-runbook.md section 4
ls -1t "${BACKUP_DIR}"/argus-*.db 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    echo "argus-snapshot: rotating out $(basename "$old")"
    rm -f "$old"
done

# Say what retention actually costs, every run. A number nobody prints is a number
# nobody notices until the disk is full.
RETAINED="$(ls -1 "${BACKUP_DIR}"/argus-*.db 2>/dev/null | wc -l | tr -d '[:space:]')"
TOTAL="$(du -sh "${BACKUP_DIR}" 2>/dev/null | cut -f1)"
echo "argus-snapshot: retaining ${RETAINED}/${KEEP} snapshots, ${TOTAL} total in ${BACKUP_DIR}"

echo "argus-snapshot: done"
