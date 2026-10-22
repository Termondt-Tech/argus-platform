# The config file names its own environment, and says which bytes are loaded

**2026-09-04. BUILT — step 1 of 4 (`src/` + config + tests; no new config KEY, so
the image deploys alone), SHIPPED as v0.3.17 on 2026-09-05. STEP 2 BUILT 2026-09-05
for v0.3.18 — the rename, the host wiring, and the deploy's config read-back; see
"Step 2, as built" below.** Design and the rejected shapes:
[`docs/designs/2026-09-04-config-profiles-and-config-only-releases.md`](../designs/2026-09-04-config-profiles-and-config-only-releases.md),
approved with all ten §9 decisions and no amendments.

Two questions with one mechanism: how a live config and a cert config coexist
without being copied over each other at deploy time, and what a **config-only
release** has to prove before it is called done.

---

## What was there

`argus.toml` was the one deployed config, edited in place. `ARGUS_ENV` **overrode**
the `environment` key. `ARGUS_CONFIG` named the file and defaulted to `argus.toml`,
resolved against the working directory. `/status` reported `environment` and
`version`; nothing anywhere reported *which config file* or *which bytes*.

Measured before changing anything, and it is the number that shaped the design:
`argus.toml`, `argus.live.toml` and `argus.dev.toml` carried **exactly the same 82
leaf keys**, and sandbox↔live differed on **three values**. The rest of the 57-line
diff was comment text. The drift problem was not the one it looked like.

---

## What is built

**`ARGUS_ENV` names the FILE. The `environment` key stays the authority. The loader
refuses when they disagree.** The override is gone. Resolution is
`ARGUS_CONFIG` (explicit path, wins) → `ARGUS_ENV` (profile →
`<ARGUS_CONFIG_DIR>/argus.<profile>.toml`) → **ConfigError**. Both set and
disagreeing is also refused, because that is a systemd unit and a compose file
edited at different times and it is otherwise silent.

**There is no default config path.** This is the half worth carrying: the old
default was `argus.toml` resolved against the working directory, which from the
repo root is the LIVE database — *that default is the mechanism of the 2026-07-21
incident*, which took `seq` 4128–4130 and wedged a running session's audit writer
for ~80 minutes. Two enforcement layers were added afterwards and the rule still
stood on discipline. It is now mechanical: a bare `python -m argus` raises, and the
eight `argus.tools.*` entry points that defaulted `--config argus.toml` now
**require** it (`loop_lag` already did). **Never re-add a default to either.**

**The profile assertion is armed by the FILENAME, so a path without one asserts
nothing** — `argus.toml`, or an ad-hoc `--config /tmp/x.toml`. `CONFIG_FILENAME_RE`
deliberately needs two dots, which is what let the mechanism ship one release ahead
of the rename. `/status` renders a profile-less file AMBER and says the check is not
armed; that is the one thing about it worth saying out loud.

**`ConfigSource` — the version stamp's missing half.** `version` answers "which
code?", and the deploy reads it back from the journal because `argus.service` has no
build step. Config is bind-mounted and *explicitly excluded from the image*
(docker-compose.yml: "config does NOT roll back with the image tag"), and nothing
answered the same question about it. So after a config-only release these four were
indistinguishable: the new config was loaded; the restart happened before the
checkout; the mount path drifted; somebody hand-edited the droplet and it has not
restarted since. `Config.load` now attaches `(profile, filename, sha)` — sha256 over
the file **bytes**, 12 hex — surfaced on `/status` (JSON `config` object, and a
"Config" row under Build) and on the startup line:

```
argus build v0.3.16 config live/argus.live.toml @35211ce0032a
```

**One line, both facts**, so the deploy script gets them from one anchor.

**A HASH, not a hand-bumped `[meta] release` string.** It cannot go stale, needs no
REQUIRED key (so introducing it forced no coupled deploy), cannot be forgotten at
tag time, and answers the sharper question — *are the bytes I deployed the bytes
that are loaded?* rather than *did somebody remember to bump it?* The git tag
already carries release identity for every class and is verified by the script's
existing checkout assertion; the hash covers what the tag cannot.

**Three full config files, pinned by a test rather than by a merge layer.**
`tests/test_config_profiles.py` asserts identical key sets, that **every profile
file actually loads** through the real loader, that value divergence is on an
explicit reasoned allowlist, and the live-specific invariants. Base+overlay was
rejected: it puts a merge step in front of a fail-closed loader, splits 82 keys of
load-bearing prose from the values it justifies, and buys that at three keys of
divergence. **The revisit threshold is asserted, not written down** — past 10
sandbox↔live entries the test itself fails and says to reopen the question.

The key-set assertion is the one that earns its keep. **Four of the last five
releases forced a coupled config move**; a REQUIRED key added to two files and not
the third is a `ConfigError` on the third, and for the live file that is a process
that will not boot, discovered at 09:29 ET on cutover day. It found a real gap while
being written (an undeclared `dev`↔`live` pair) before a human read it.

---

## `argus.live.toml`, regenerated — and the cutover blocker it was hiding

The file existed but was not a live config. Reading it beside `argus.toml` produced
five findings; two are fixed here and three are recorded.

**F1 — live and sandbox wrote the SAME database, and this is a cutover blocker.**
Both carried `db_path = "argus.db"`. The Dockerfile sets `WORKDIR /data` precisely
so that relative path lands in the single `argus-state` volume, so
`environment = "live"` would have opened **the cert database**: the hash chain
continues from sandbox rows, and `positions` — which reconciliation reads back as
expected state — would be cert rows compared against the **live** account, i.e.
`missing_at_broker` on day one, which in live is a process-wide hard halt, not a
lenient abort. The sandbox 24-h wipe keys on `environment == "sandbox"` and would
have stopped clearing it, so the residue is permanent rather than self-limiting.
Fixed: `argus-live.db`, with the cert file deliberately left in place beside it so
`tools.mark` / `tools.desk` keep reading the cert-era record. **This is also the
strongest argument for the profile model** — one file edited in place at cutover
cannot express "the live database is a different file", because there is only ever
one file.

**F2 — the live file bundled P5's auto-cancel promotion into the cutover.**
`unmanaged_order_cancel = true`, a path recorded as never having fired. Reset to
`false`; it is promoted in **cert** first, as its own config-only release — which
doubles as the first real exercise of the release path this work adds.

**F3 — the live file carries cert risk sizing** (2 × 1250 = 2500). The welded
invariant holds exactly, so the file is coherent; the numbers are still the ones
chosen for cert, and the next gate is live-**min**-size. Left as-is deliberately;
decided at that gate, not here.

**F4 — the `ARGUS_ENV=live` override was fenced only by accident.** It refused
against `argus.toml` solely because `account_stream_max_age_s = 120 > 30` tripped
the live ceiling — and that 120 is a *cert artifact* that could legitimately drop.
Removing the override removes the reliance on a fence that was never designed to be
one.

**F5 — `argus.dev.toml` contradicts its own note** (`signal.max_width = 25.0`
against a `walk_source` line saying to keep values in step). Harmless, dev submits
nothing — allowlisted with that reasoning rather than left silent, and flagged to
narrow next time the dev config is touched.

---

## Rules worth carrying

- **The profile names the file; `environment` decides behaviour.** Never let a
  selector override the key — that is a second answer to "are we routing real
  money?", and the codebase already had three names for it.
- **No default config path, in the app or in a tool.** A default is how the wrong
  database gets opened.
- **A copy into a target with `None` defaults cannot fail loudly** (2026-09-02's
  lesson, applied ahead of time here): the fingerprint is driven from a real
  `Config.load` through to rendered HTML and JSON in `tests/test_status.py`, not
  asserted on a hand-built snapshot that would pass forever after
  `Application.startup` quietly stopped passing the values.
- **A config release is still a restart, and must stay one.** Config is read once at
  startup. **Do not add hot-reload** — risk rails changing under a live position is
  strictly worse than a scheduled restart, and half the config is read into frozen
  objects at session build.
- **`/status` colour follows what is ACTIONABLE, not what is unusual** — the rule
  `_version_cell` already binds. The expected state (a named profile and a sha) is
  plain text.

---

## Step 2, as built (2026-09-05)

**`argus.toml` ceases to exist.** It is `argus.sandbox.toml`, and the old name is
in `.gitignore` — a stray copy (a hand-made backup, a half-finished cutover, muscle
memory) must not be committable, because it would carry no profile and arm no
assertion. `PROFILE_FILES["sandbox"]` moved with it, and the drift module gained a
test that fails if a FOURTH `argus.<profile>.toml` appears without being listed:
an unlisted profile file sits outside every assertion in that module, which is the
state the module exists to prevent, one file over.

**The profile reaches the container from a file that is not in git.**
`argus.service` gains `EnvironmentFile=/etc/argus/host.env` — root-owned, 600, per
host, **no `-` prefix** so a missing file fails the UNIT loudly rather than starting
it profile-less and letting compose refuse one layer further from the cause. Compose
interpolates `${ARGUS_ENV:?}` (**never** `${ARGUS_ENV:-sandbox}`; a default here is
exactly how the wrong config gets deployed quietly) into the container environment
and into the mount `./argus.${ARGUS_ENV}.toml:/etc/argus/argus.${ARGUS_ENV}.toml`.
`ARGUS_CONFIG` is deliberately UNSET there: an explicit path WINS, so setting it
would leave the profile decorating a filename it no longer selects, and `/status`
could no longer report which profile is armed. **The live cutover is now one line of
one file changing on one host** — not a commit, not a rename, not a copy.

**The IMAGE carried two defaults of its own, and one of them is the single failure
the fingerprint cannot see.** `Dockerfile` set `ENV ARGUS_CONFIG=/etc/argus/argus.toml`
and baked `COPY argus.toml /etc/argus/argus.toml`. The ENV **wins over the profile**
(an explicit path beats `ARGUS_ENV`, correctly), so with compose's `ARGUS_CONFIG`
removed the image's own would silently have taken over — an image-level default
config path, the shape step 1 deleted from the code, one layer down. The baked copy
is worse in a subtler way: it sat at the same path the bind mount targets, so a
drifted or missing mount fell back to it, and **because both copies come from one
git checkout the sha would MATCH** — the read-back would pass a config that was
never mounted. Both are gone; nothing is baked, and a drifted mount is now a hard
`ConfigError` on every profile. **Never re-add either.**

**Two consequences worth carrying.** Every `docker compose` command on that host now
needs `ARGUS_ENV` — `build`, `run`, `ps`, `down`, not just `up` — and **sudo scrubs
the environment**, so it goes on sudo's own command line. And a profile with no
matching file is not a missing-file error: Docker creates a missing bind-mount
source as a **DIRECTORY**, so `Config.load` reached `IsADirectoryError` and read as a
crash. It now answers `OSError` with its own fail-closed sentence, because the
loader's refusals are the process's.

**`scripts/argus-deploy.sh` verifies the result.** It resolves the profile from the
same `host.env` systemd reads (never a default, never inferred from the branch),
prints it in the plan, refuses a tag that does not contain `argus.<profile>.toml`,
names a `CONFIG_INPUTS` release class so the plan *states* which class this is
rather than leaving it deduced from the absence of a build, computes the expected
sha from the tag AND re-checks it against the working tree after checkout (the mount
reads the tree, not the git object), diffs the config on `--dry-run`, and **reads
the fingerprint back after the restart on EVERY release class** — profile, filename
and sha, off the same `argus build` line the version read-back already anchors on.
The profile/filename half is what catches a drifted mount or a stale `ARGUS_CONFIG`
in `./.env`; the sha half is what a config-only release has instead of a version.

**The migration's own deploy is the fourth thing that had to change, and it was
found by failing.** `scripts/argus-deploy.sh` runs out of the WORKING TREE, so the
script deploying v<N+1> is the one from v<N> — and step 2 was the first release ever
to change what the steps AFTER the checkout must do. v0.3.17's script checked out
v0.3.18's compose file and then ran its own unprefixed `docker compose build`,
which died on `${ARGUS_ENV:?}`; the droplet was left with the new tree, the OLD
systemd unit, nothing built, and a `systemctl restart` away from a crash loop. Fixed
in v0.3.19 by handing over to the script the tag ships, immediately after the
checkout verification, **carrying `PREV_TAG`** — without which the handed-over run
re-derives an empty diff and skips a build the first run had planned, re-creating
the silent-reattach failure the version read-back exists to catch. **Rule worth
carrying: a release that changes how its own deploy steps must run cannot be
deployed by the previous release's script — either hand over, or expect to finish
that one by hand.**

**`tests/test_deploy_wiring.py` pins all four non-Python files** — compose refuses
rather than defaults, compose sets no `ARGUS_CONFIG`, the mount keeps the profile in
the filename, the unit's `EnvironmentFile` has no `-`, the deploy compares rather
than merely prints, its sha width equals `CONFIG_SHA_LEN`, and **the config
read-back is NOT inside the `NEED_BUILD` guard** the version read-back correctly
sits in. Same reasoning as `tests/test_build_version.py`: prose and script are two
implementations of one procedure, and only one of them gets read by a human who can
notice.

---

## Still owed — steps 3 and 4

1. **The cert config-only release** flipping `unmanaged_order_cancel` (F2). It is
   the promotion P5 is waiting for AND the first real exercise of the config-only
   release path, now that the path finally asserts something. `argus.live.toml`
   inherits `false` until cert has shown the re-cancel actually cancel something —
   a test asserts `false` in all three files, so the flip costs an explicit edit.
2. **The live cutover**, which by then loads a file a test has been loading since
   step 1, on a release path exercised in cert, by changing one line of
   `/etc/argus/host.env` and restarting. **`host.env` must exist on the droplet
   BEFORE the v0.3.18 deploy** — the unit will not start without it, which is the
   intended direction of that failure but a bad surprise at 09:29 ET.
