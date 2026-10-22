# Design — config profiles, and what a config-only release has to prove

**Status: APPROVED 2026-09-04 — all ten §9 decisions accepted with no amendments.
§8 STEP 1 IS BUILT the same day and SHIPPED as v0.3.17 (2026-09-05). §8 STEP 2 IS
BUILT 2026-09-05 for v0.3.18 — the rename, the host wiring, and §7's five
`argus-deploy.sh` changes, which belong to step 2 because step 3 is defined as the
first EXERCISE of the config-only path and cannot exercise a check that does not
exist. Steps 3–4 (the cert flip, the cutover) are staged and not yet done.** The
normative record is
[`docs/decisions/2026-09-04-config-profiles.md`](../decisions/2026-09-04-config-profiles.md).

**One deviation from §8, taken deliberately:** step 1 also regenerates
`argus.live.toml` with the F1 (`db_path`) and F2 (`unmanaged_order_cancel`) fixes,
which §8 had placed in step 2. The file is inert until step 2 wires it up — nothing
loads it — and §5.2's drift test needs a *correct* live file to assert against, so
deferring it would have meant shipping the test against a config the test itself
says is wrong. Nothing else moved between steps.

**Status when written: PROPOSED 2026-09-04, awaiting sign-off.**

Working arrangement, as for the last four designs: **work on `main`, do not commit,
do not push.** Richard reviews the working tree.

Two questions, one answer, because they share a mechanism:

1. **Environment-selected config files** — stop copying/renaming `argus.toml` at
   deploy; name files by environment and pick one with an env var.
2. **Config-only releases** — what they are, what verifies them, and why they
   currently verify *nothing*.

---

## 0. What this changes, in one paragraph

`argus.toml` is renamed `argus.sandbox.toml` and stops existing under its old name;
`ARGUS_ENV` stops being an *override of* the `environment` key and becomes the
*selector of* the config file, with the loader asserting the two agree; the default
config path is **deleted**, so a bare `python -m argus` refuses to start instead of
opening the live database; the three profile files stay full standalone files and
their divergence is pinned by a test rather than by a merge layer; `/status` and the
startup log gain a **config fingerprint** (profile, filename, sha256 prefix), which
is the version stamp's missing half — the version stamp covers the image, and config
is bind-mounted and deliberately does *not* roll back with the image tag; and
`argus-deploy.sh` reads that fingerprint back after every restart, which gives a
config-only release the post-deploy assertion it has none of today.

---

## 1. Recommendation in one paragraph

**One name, one meaning, and the file on disk stays the whole truth.** Do not add a
second environment concept (`APP_ENV` beside `environment`) — Argus already has two
switches that both spell "live" and a third that can override one of them, and a
third spelling is the failure this codebase's non-negotiables exist to prevent. Make
`ARGUS_ENV` name the profile, make the profile name the filename, and make the
loader refuse when the file's own `environment` key disagrees with the profile it
was loaded as. Reject a base+overlay merge layer: the config loader is fail-closed
and 82 keys of load-bearing prose, and "read the file to know what is running" is
worth more than DRY here. Pin drift with a test that asserts identical key sets
across profiles and an explicit allowlist of intended value divergence — the same
move `tests/test_build_version.py` already makes for the build arg.

---

## 2. What is actually there today (measured, not assumed)

| Fact | Value |
|---|---|
| `argus.toml` | 54,755 B, 82 leaf keys, `environment = "sandbox"` |
| `argus.live.toml` | 52,605 B, 82 leaf keys, `environment = "live"`, untracked |
| `argus.dev.toml` | 10,125 B, 82 leaf keys, `environment = "shadow"` |
| Key-set drift | **none** — all three carry exactly the same 82 keys |
| `argus.toml` → `.live.toml` value drift | **3 keys** (below) |
| `argus.toml` → `.dev.toml` value drift | 13 keys |
| Config path resolution | `__main__.py:35` — `os.environ.get("ARGUS_CONFIG", "argus.toml")` |
| `ARGUS_ENV` today | `config.py:909` — **overrides the `environment` key**, does not select a file |
| Tools | 8 of 9 default `--config argus.toml`; only `loop_lag` requires it |
| Container | compose sets `ARGUS_CONFIG: /etc/argus/argus.toml`, bind-mounts `./argus.toml` |
| Deploy classes | `BUILD_INPUTS=(src requirements.txt pyproject.toml Dockerfile)`; everything else ⇒ "config-only" ⇒ **skip build, skip the version read-back** |
| Runtime config identity | **none.** `/status` reports `environment` and `version`; nothing says which file, or which bytes |

### 2.1 The live↔sandbox divergence, in full

```
key                                    sandbox      live
environment                            sandbox      live
staleness.account_stream_max_age_s     120.0        30.0
watchdog.unmanaged_order_cancel        false        true
```

**That is the entire semantic drift.** The remaining 57 lines of `diff` are comment
text. You do **not** need to start over from `argus.toml`; you need to regenerate
`argus.live.toml` from it and re-apply three lines — and then decide the four
findings in §3, which are the ones that actually matter.

---

## 3. Findings that are independent of the file-naming question

These surfaced while reading the two configs side by side. Each is a live-flip
concern in its own right, and none of them is fixed by renaming anything.

### F1 — live and sandbox write the SAME database. This is a cutover blocker.

Both files carry `audit.db_path = "argus.db"`. The Dockerfile sets `WORKDIR /data`
(line 109) precisely so that relative path resolves inside the state volume, and
compose mounts the single named volume `argus-state` at `/data`. So
`environment = "live"` would open **`/data/argus.db` — the cert database**, and:

- the hash chain would continue from cert records, so the live record has no clean
  origin and `seq` 1 is a sandbox row;
- `positions` is what reconciliation reads back as expected state
  (CLAUDE.md, "Live requires durable audit writes"), so cert-era rows would be
  compared against the **live** account and produce `missing_at_broker` on day one
  — which in `live` is a process-wide hard halt, not a lenient abort;
- the sandbox 24-h wipe keys on `environment == "sandbox"` and would stop firing,
  so the residue is permanent rather than self-clearing.

**Fix:** `argus.live.toml` needs its own `db_path` (`argus-live.db`), and a decision
on the cert chain — archived off the volume, or left beside it. Recommend leaving it
in place and naming the new file; it costs a few hundred MB and keeps the cert
record readable by `tools.mark` / `tools.desk` for the comparisons this phase is
still producing.

This finding is also the strongest argument *for* the profile model: a single
`argus.toml` edited in place at cutover has no way to express "the live database is
a different file", because there is only ever one file.

### F2 — the live file bundles P5's auto-cancel promotion into the cutover

`watchdog.unmanaged_order_cancel = true` in `argus.live.toml`. CLAUDE.md records
the re-cancel as **never proven**: P5 shipped in `would_cancel` mode, the residual
registry has been observed saving a trade idea end to end, and the cancel itself has
not fired. Setting it here means the first real fire of that path happens on the same
day as the first real order. That is two unproven things at once.

**Recommend:** flip it in **cert** first, as its own config-only release — which is
exactly the release class this design is about, and a good first exercise of it —
and let `argus.live.toml` inherit `false` until cert has shown it cancel something.

### F3 — the live file carries cert risk sizing

`max_loss_per_position = 1250`, `max_concurrent_positions = 2`,
`daily_max_loss = 2500`. The welded invariant holds exactly (2 × 1250 = 2500), so
the file is *coherent* — but these are the numbers chosen for cert, and the next
phase gate is **live-min-size**, not live-at-cert-size. Decide them, don't inherit
them. (Note the breaker cannot be validated in cert at all — day P&L is structurally
zero there — so live-min-size is also the first time that rail is real.)

### F4 — the `ARGUS_ENV=live` override is fenced today only by accident

`ARGUS_ENV=live python -m argus` against today's `argus.toml` refuses to start, but
only because `account_stream_max_age_s = 120 > 30` trips `config.py:984`. That
value is a **cert artifact** (CLAUDE.md: cert's `/positions` timing out). The day it
drops to ≤ 30 for any reason, an env var starts being able to route the sandbox
config's orders at the live API, silently. §4 removes the override rather than
relying on a fence that was never designed to be one.

### F5 — `argus.dev.toml` contradicts its own note

`walk_source` in the dev file says *"keep the VALUES in step with argus.toml so a dev
run reproduces production's walk"*, while `signal.max_width` is `25.0` against
`10.0`. Harmless (dev submits nothing) but it is exactly the class of drift §5's
test is for, so it should be either fixed or allowlisted with a reason.

---

## 4. The model

### 4.1 Three names, and why not four

Argus already has, today:

| Name | Means | Where |
|---|---|---|
| `environment` (config key) | **the trading environment** — drives `TastytradeSession.is_test`, the reconciliation halt-vs-lenient boundary, the sandbox wipe, the `audit.synchronous` fence, the `account_stream_max_age_s` fence | `argus.toml`, authority per CLAUDE.md |
| `ARGUS_ENV` | overrides the above | `config.py:909` |
| `ARGUS_CONFIG` | which file to load | `__main__.py:35` |

Adding `APP_ENV` as a fourth would give three variables that can each independently
answer "are we routing real money?", two of which can disagree with the file. The
recommendation is to go the other way: **collapse to two**, and make them assert
each other.

| Name | Means, after |
|---|---|
| `environment` (config key) | unchanged — still the sole authority on trading behaviour |
| `ARGUS_ENV` | **the profile: which file.** No longer overrides anything |
| `ARGUS_CONFIG` | unchanged — an explicit path, and it wins |

### 4.2 Resolution order

```
1. ARGUS_CONFIG set        -> use that path verbatim.        (explicit beats derived)
2. else ARGUS_ENV set      -> <ARGUS_CONFIG_DIR>/argus.<ARGUS_ENV>.toml
                              ARGUS_CONFIG_DIR defaults to the working directory
3. else                    -> ConfigError. REFUSE TO START.
```

**Step 3 is the point, not a detail.** Today `python -m argus` from the repo root
defaults to `argus.toml` — that default *is* the mechanism of the 2026-07-21
incident (80 minutes of wedged audit writer, ~2,000 Phase 0 records lost). The
NEVER-TOUCH-LIVE-DB rule currently stands on discipline plus two enforcement layers.
Deleting the default, and deleting the *filename* `argus.toml` along with it, makes
the bare command a `FileNotFoundError` instead of a production open. Same for the
eight tools that default `--config argus.toml`: make all of them `required=True`,
matching `loop_lag`.

That is a real, mechanical retirement of a standing landmine, and it falls out of
this work for free. It is arguably the best reason to do the work at all.

### 4.3 The profile↔environment assertion

```
if profile in VALID_ENVIRONMENTS and profile != environment:
    raise ConfigError(...)
```

So `argus.live.toml` must declare `environment = "live"`, `argus.sandbox.toml` must
declare `sandbox`, and the pairing cannot be got wrong by editing one and not the
other. `dev` is not in `VALID_ENVIRONMENTS` (`shadow`, `sandbox`, `live`), so
`argus.dev.toml` is free to declare `shadow` — with one extra rail, since this is the
file most likely to be run by hand from a laptop:

```
if profile == "dev" and environment == "live":
    raise ConfigError(...)
```

An explicit table would also work; a two-line rule is easier to keep true.

### 4.4 File layout

```
argus.dev.toml        environment = "shadow"    db_path = dev/argus-dev.db
argus.sandbox.toml    environment = "sandbox"   db_path = argus.db        (was argus.toml)
argus.live.toml       environment = "live"      db_path = argus-live.db   (F1)
```

`argus.toml` ceases to exist. Add it to `.gitignore` so a stray copy cannot be
committed and cannot be found by muscle memory.

### 4.5 Container and host wiring

The profile must reach the container, and it must not come from `.env` — `.env` is
hand-edited, is not touched by the deploy, and is already the documented trap for
`ARGUS_IMAGE_TAG` (compose comment, and runbook §17).

**systemd** (`argus.service`, git-tracked, synced by the deploy script):

```
EnvironmentFile=/etc/argus/host.env
```

**`/etc/argus/host.env`** — root-owned, mode 600, **per host, not in git**, one line:

```
ARGUS_ENV=live
```

No `-` prefix on `EnvironmentFile`, so a missing file fails the unit loudly rather
than starting with an unset variable. Provisioned once, beside the secrets, in
runbook §8.

**compose:**

```yaml
environment:
  ARGUS_ENV: ${ARGUS_ENV:?ARGUS_ENV must be set — see /etc/argus/host.env}
  ARGUS_CONFIG_DIR: /etc/argus
  # ARGUS_CONFIG deliberately UNSET: the profile derives the path, so the
  # filename inside the container keeps the profile in its name and /status
  # can report it.
volumes:
  - ./argus.${ARGUS_ENV}.toml:/etc/argus/argus.${ARGUS_ENV}.toml:ro
  - ./blackout_dates.txt:/etc/argus/blackout_dates.txt:ro
```

`${VAR:?message}` makes compose **refuse** rather than interpolate empty — the
fail-closed shape, and the reason not to write `${ARGUS_ENV:-sandbox}`. A default
here would be how you deploy the wrong config quietly, which is the whole failure
class this design exists to close.

Note the mount keeps the *profile in the filename inside the container*. Mounting to
a fixed `/etc/argus/argus.toml` would work too and would be one fewer moving part,
but the process would then have no way to report which file it read, and §6 depends
on it being able to.

---

## 5. Keeping three full files from drifting: a test, not a merge layer

### 5.1 Rejected: base + overlay

`argus.base.toml` + per-profile deltas, deep-merged by the loader. Rejected on three
grounds:

- It puts a merge step **in front of a fail-closed loader**. Every `_require` /
  `_positive_float` / cross-key check (`guard_timeout_s <= http_timeout_s`,
  `status_poll_interval_s <= working_timeout_s`, the live fences) currently reasons
  about one flat dict from one file. A merge introduces a state where no file on
  disk describes what is running.
- The config is **82 keys and ~900 lines of load-bearing prose** — the reasoning for
  `entry_walk_away_pct`, `sustained_s`, `account_position_evict_after_s` and the rest
  lives in the comments next to the values. Splitting values from the prose that
  justifies them is a real loss, and it is a loss taken at exactly the moment
  (a live cutover) when you most want to read one file top to bottom.
- The drift being solved is **3 keys**. That is not a DRY problem.

### 5.2 Recommended: `tests/test_config_profiles.py`

Four assertions, all cheap, all failing loudly on the next coupled config move:

**(a) Identical key sets.** Already true today (82/82/82). This is the one that
matters most: four of the last five releases forced a coupled config move
(`exit_unattempted_max_s`, `[reconcile]`, `book_confirm_ttl_s`,
`unmanaged_order_max_s`, `exit_walk_min_steps`). Adding a REQUIRED key to two of
three files is a `ConfigError` on the third — and on a live cutover that is a process
that will not boot, discovered at 09:29 ET.

**(b) Every profile file loads.** `Config.from_dict(tomllib.load(f))` for each,
asserting no raise. This is (a)'s teeth: it catches a missing key, a broken cross-key
constraint, and a live fence violation, in the file that is not being run today.

**(c) Value divergence is on an explicit allowlist**, each entry carrying a reason:

```python
ALLOWED_DIVERGENCE = {
    # sandbox <-> live
    ("sandbox", "live", "environment"):
        "the profile assertion (config.py) requires this",
    ("sandbox", "live", "audit.db_path"):
        "live must not continue the cert hash chain — F1",
    ("sandbox", "live", "staleness.account_stream_max_age_s"):
        "120 is a cert artifact; live is fenced at <=30 by config.py:984",
    ...
}
```

Anything else differing fails the test. Adding a divergence is then a deliberate,
reviewed, one-line act rather than a silent edit.

**(d) Live-specific invariants**, asserted against `argus.live.toml` specifically —
`environment == "live"`, `audit.synchronous in DURABLE_SYNCHRONOUS`,
`db_path != sandbox's db_path`, and
`max_concurrent_positions * max_loss_per_position <= daily_max_loss`. The last one
is the welded invariant CLAUDE.md names; it is checked by prose today and by nothing
executable.

This is the same shape as `tests/test_build_version.py` ("no executable path may
build the image without passing `ARGUS_VERSION`") and the AST sweeps: convert a rule
that lives in a sentence into one that lives in a red test.

**Revisit threshold:** if the sandbox↔live allowlist passes ~10 entries, the DRY
argument starts to win and §5.1 should be reopened. At 3 it does not.

---

## 6. Config identity at runtime — the version stamp's missing half

The version stamp answers *"which code is running?"* and the deploy script reads it
back from the journal, because `argus.service` has no build step and a silent
reattach to the previous image is otherwise invisible.

**Nothing answers the same question about config**, and config is the half that is
*explicitly excluded* from the image — compose says so: *"config does NOT roll back
with the image tag."* So today, after a config-only release, there is no fact
anywhere — not on `/status`, not in the journal, not in the script's exit code —
that distinguishes:

- the new config was loaded ✅
- the restart happened before the checkout, so the **old** config is loaded
- the mount path drifted and the container is reading a stale file
- somebody hand-edited the droplet's config and the process has not restarted since

All four look identical. And `/status`'s `version` field, on a config-only release,
*legitimately reports the previous tag* — the script documents this and skips the
read-back for exactly that reason. Correct, and it leaves the release class with
zero verification.

**Add three fields**, computed at load and carried on `Config`:

| Field | Value |
|---|---|
| `config_profile` | `"live"` |
| `config_file` | `"argus.live.toml"` (basename only — never the full path, which leaks nothing useful and adds container-path noise) |
| `config_sha` | first 12 hex of `sha256` over the file **bytes** |

Surfaced in three places, mirroring how `version` is surfaced:

- `/status` JSON, **top level beside `environment` and `version`** — same reasoning
  the `version` comment already gives ("a script asking what is deployed should not
  have to know it lives under `process`").
- The `/status` HTML, as a row under Build.
- The startup log line: `argus build v0.3.16 config live/argus.live.toml @3f9a1c2e5b71`
  — extending `app.py:491`'s existing line rather than adding a second one, so the
  deploy script's journal grep gets both facts from one anchor.

**Why a hash and not a hand-maintained `[meta] release = "v0.3.17"` key:** a hash
cannot go stale, needs no REQUIRED key (so it forces no coupled deploy to
introduce), cannot be forgotten at tag time, and answers the sharper question — *are
the bytes I deployed the bytes that are loaded?* A version string answers "did
somebody remember to bump it". The git tag already carries release identity for all
three classes and is verified by the script's existing checkout assertion; the hash
covers the part the tag cannot. If a human-readable string on `/status` is still
wanted, `[meta] release` can be added later as cosmetic — but it must then be
*verified* by the script, not merely displayed, or it becomes one more thing that can
be confidently wrong.

---

## 7. Release classes, and what each one must prove

Three classes. All three take a `vX.Y.Z` git tag — that is already true today, and
is not the gap.

| Class | Inputs | Actions | Post-deploy assertion |
|---|---|---|---|
| **code** | `src/`, `requirements.txt`, `pyproject.toml`, `Dockerfile` | build + stamp + tag image + restart | version read-back **and** config read-back |
| **config** | `argus.*.toml`, `blackout_dates.txt` | checkout + restart | **config read-back** ← new; today: nothing |
| **host** | `argus.service`, `docker-compose.yml` | unit sync / daemon-reload / restart | config read-back |

The classes are not exclusive — most releases are code+config, which is the coupled
deploy CLAUDE.md keeps naming. `argus-deploy.sh` already detects code-vs-not
correctly; the changes needed are:

1. **`CONFIG_INPUTS=(argus.dev.toml argus.sandbox.toml argus.live.toml blackout_dates.txt)`**
   as a named class, so the plan output says `config: YES (argus.live.toml)` instead
   of inferring it from the absence of a build. An operator reading the plan should
   see which class this is, not deduce it.
2. **Resolve and print the profile in the plan**: read `ARGUS_ENV` from
   `/etc/argus/host.env`, refuse if unset, refuse if
   `argus.<profile>.toml` does not exist in the checked-out tree. That last check is
   the one that catches a tag that renamed or dropped the profile file — a class of
   failure that currently presents as a crash loop at restart.
3. **Compute the expected sha before restart**, from the checked-out file:
   `sha256sum argus.<profile>.toml | cut -c1-12`.
4. **Read it back after restart**, from the same `--since $RESTART_AT` journal query
   the version read-back already uses, and **fail the deploy on mismatch** — on
   *every* class, unlike the version read-back which is correctly skipped on
   config-only.
5. **`--dry-run` diffs the config**: `git diff $PREV_TAG $VERSION -- argus.<profile>.toml`.
   For a config-only release that diff *is* the release, and reading it before
   deploying is the whole review.

### 7.1 One thing that does not change: a config release is still a restart

Config is read once at startup. A config-only release therefore tears down the
session, the watchdog, the virtual stops and the marks, exactly like a code release
— so it carries the same timing constraint, and the same "never mid-session with a
position open" rule.

**Do not add hot-reload.** Risk rails changing under a live position is a strictly
worse failure than a scheduled restart, and half the config is read once into frozen
objects at session build. The restart cost is the honest one.

---

## 8. Migration order

Ordered so that nothing is ever in a state where the running droplet cannot be
rolled back.

1. **Code release (v0.3.17)** — the loader changes, the profile assertion, the
   config fingerprint, `/status`, `app.py`'s startup line, the nine tools'
   `required=True`, and `tests/test_config_profiles.py`. Ship it while the config
   files still have their current names and `ARGUS_CONFIG` is still set explicitly by
   compose, so this release changes **no** deployed behaviour beyond adding fields.
   Verify the fingerprint appears on `/status`.
2. **Rename + host wiring (v0.3.18)** — `argus.toml` → `argus.sandbox.toml`,
   `argus.live.toml` regenerated from it with the §3 fixes, `.gitignore` entry,
   `docker-compose.yml` and `argus.service` updated, `/etc/argus/host.env`
   provisioned on the droplet **before** the deploy. Runbook §17 updated in the same
   commit. This is a code+config+host release and it is the one that needs care:
   `host.env` must exist first, or the unit fails to start.
3. **Cert config-only release (v0.3.19)** — flip `unmanaged_order_cancel = true` in
   `argus.sandbox.toml` alone (F2). Purpose is twofold: it is the promotion P5 is
   waiting for, and it is the **first real exercise of the config-only release path**
   including the new read-back — proved in cert, on a change that is already
   designed and already instrumented, before that path is trusted for a cutover.
4. **Live cutover** — `host.env` flipped to `ARGUS_ENV=live`, restart. At that point
   the live config has been loaded by a test since step 1, its key set has been
   pinned since step 1, and the release path it arrives on has been exercised in
   cert.

Steps 1 and 2 could be one release. Keeping them apart means step 1 is verifiable in
isolation, and step 2's blast radius is a rename you can roll back by tag.

---

## 9. Decisions for Richard

| # | Decision | Recommendation |
|---|---|---|
| D1 | `ARGUS_ENV` repurposed as the profile selector, override semantics **deleted** | Yes. One name, one meaning; F4 shows the current fence is accidental |
| D2 | No default config path — bare `python -m argus` refuses | Yes. Retires the 2026-07-21 landmine mechanically |
| D3 | Three full files + a drift test, vs base+overlay | Full files + test; revisit at ~10 allowlist entries |
| D4 | Config fingerprint = sha, not a hand-bumped `[meta] release` | sha. No new REQUIRED key, nothing to forget |
| D5 | `db_path` split for live (F1) | Yes — this is a cutover blocker, not a preference |
| D6 | `unmanaged_order_cancel` promoted in cert first, not bundled into the cutover (F2) | Yes |
| D7 | Live risk sizing decided rather than inherited from cert (F3) | Decide at the live-min-size gate; out of scope here |
| D8 | `ARGUS_ENV` from `/etc/argus/host.env`, not `.env`, with compose `${VAR:?}` | Yes |
| D9 | No config hot-reload | Yes |
| D10 | Config read-back fails the deploy on **every** release class | Yes — it is the only assertion a config-only release has |

---

## 10. What this deliberately does not do

- **No `[meta]` section, no version string in config.** §6.
- **No change to the `environment` key's authority.** It still decides
  `is_test`, the halt boundary, the wipe, and the two live fences. The profile only
  decides which file is read, and then asserts agreement.
- **No merge/inheritance in the loader.** §5.1.
- **No hot-reload.** §7.1.
- **No change to what a git tag means.** All three release classes are tagged today
  and stay tagged; what changes is that all three now get verified.
