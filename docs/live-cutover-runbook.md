# Argus live cutover — sandbox → live-min-size

> # ✅ PERFORMED 2026-09-15, 16:30 ET. ARGUS IS LIVE.
>
> **This document is now a RECORD and a ROLLBACK procedure, not a plan.** The flip
> was executed on the tag **v0.4.2**, profile `live`, config sha `98fcd21981a3`,
> against production tastytrade account `5XX00000`. Everything below stands as
> written; the per-section notes say what actually happened.
>
> | Step | Outcome |
> |---|---|
> | §4 pre-flight | Clean. Profile `sandbox` confirmed, tree clean at `v0.4.2`, `account_number` / `environment` / `db_path` all as expected, sha `98fcd21981a3` recorded, `./.env` carried no overriding variable, `credcheck` clean on all three slots in the **running** container, cert-final snapshot taken and moved out of the rotation glob (`cert-final-20260915.db`, 810,552 records, chain intact) |
> | §5.2 the flip | `/etc/argus/host.env`: `ARGUS_ENV=sandbox` → `ARGUS_ENV=live`, 16:30:36 ET |
> | §5.3 the restart | `scripts/argus-deploy.sh v0.4.2` at 16:33. Plan read `profile: live (argus.live.toml, sha 98fcd21981a3) — from /etc/argus/host.env`, `build: no`, `restart: YES`. Both read-backs passed |
> | §5.4 the boot | `argus build v0.4.2 config live/argus.live.toml @98fcd21981a3`, `acquired write lock on argus-live.db`, `execution stack ready: … tastytrade (account 5XX00000)`, **`broker reconciliation clean`**, `argus started in live mode`. `/status` returned `environment: "live"`, `credentials.trading: TASTYTRADE_TRADING_LIVE_*`, `account.pinned: true`, `halted: false`, `kill_switch.engaged: false`, `next_session_open: 2026-09-16T09:40:00-04:00` |
> | §5.5 kill switch | **Exercised on the live process** — `kill_switch.trip` 16:45:13 ET, `kill_switch.resume` 16:45:37 ET, both Tier 1 on the live chain. Cleared with `POST /resume?confirm=yes`, not a restart. `engaged: false`, `restored: false`, no `HaltRecord` left behind |
>
> **The two things §9 said could not be verified in advance are now closed by
> doing them.** `startup_reconcile` opens a real trading session, so the clean
> reconcile at 16:33:18 is simultaneous proof that (1) the live OAuth app
> registration **authenticated against the production API** — the failure
> `credcheck` reports shape but never authentication for — and (2) the live account
> **reconciles clean**, which is also C8 confirmed by machine rather than by eye
> (no position discrepancies, no order discrepancies, no unmappable orders).
> **G2 is closed by performance**: §5.1 makes the post-close flip the rehearsal.
>
> **Day 1 is 2026-09-16, a known FOMC blackout day — Argus will place no entries.**
> See §6, which now says what that does and does not prove.
>
> The rollback in §7 remains live and unchanged. **Never roll back with a live
> position open.**

**One-time migration.** Moves the droplet from trading tastytrade's cert
environment to trading the real account, at minimum size. This is
[`docs/go-live-findings.md`](go-live-findings.md) **G2**, and step 4 of the
config-profiles migration.

> **Three cutover documents exist. This is the only live one.**
>
> | Document | What it is |
> |---|---|
> | **this file** | sandbox → live. **PERFORMED 2026-09-15** — now the record of that cutover and the rollback procedure for it. |
> | `argus-deployment-runbook.md` §16 (private record, not published) | **Chunks 1–4 cutover**, done 2026-07-27: timer model → always-on supervisor. Historical. Nothing to do with `environment`. |
> | `argus-cutover-runbook.md` (private record, not published) | A superseded draft of §16. A pointer file only. |
>
> This file does **not** re-document provisioning, credentials, the build, or the
> ordinary deploy. Those are runbook §8 (private record, not published),
> §11 (private record, not published)
> and §17 (private record, not published),
> and they are correct. What is here is only what is *specific to changing
> worlds*, plus the sequencing that makes each step's failure cheap.

---

## 0. The mechanism, in one paragraph

`/etc/argus/host.env` holds one line, `ARGUS_ENV=<profile>`. `argus.service`
reads it via `EnvironmentFile=` (no `-` prefix, so a missing file fails the unit);
`docker-compose.yml` interpolates `${ARGUS_ENV:?}` into both the container
environment and the config bind-mount path `./argus.${ARGUS_ENV}.toml`. The
profile names the **file**; the file's own `environment` key remains the sole
authority on trading behaviour, and the loader refuses to start when the two
disagree. **So the cutover is that one line changing from `sandbox` to `live`,
followed by a restart.** No commit, no rebuild, no image change, no `.env` edit,
no rename. The rollback is the same line changing back.

Nothing else can substitute for it: the image carries neither a baked config nor
an `ARGUS_CONFIG`, and `ARGUS_CONFIG` is deliberately unset in compose — an
explicit path *wins* over the profile and would leave `ARGUS_ENV` decorating a
filename it no longer selects.
→ [config profiles](decisions/2026-09-04-config-profiles.md)

---

## 1. What the flip actually changes

Eight declared config divergences, two of them prose. Verified against
`argus.sandbox.toml` / `argus.live.toml` at the time of writing; the authority is
`tests/test_config_profiles.py`'s `ALLOWED_DIVERGENCE`, which fails the suite if
this list and the files disagree.

| Key | sandbox | live | Consequence |
|---|---|---|---|
| `environment` | `"sandbox"` | `"live"` | `TastytradeSession.is_test` → `False`. **The only thing that can reach the production API.** |
| `broker.account_number` | `""` (unpinned) | `"5XX00000"` | The supervisor filters to that account. `config.py` refuses `live` without it. |
| `risk.position_size_pct` | `0.02` | `0.03` | Relative size rail. Implies a **graded NLV floor, not a single one** — see §1.2. |
| `staleness.account_stream_max_age_s` | `120.0` | `30.0` | The 120 is a **cert artifact** (cert's `/positions` latency) and `config.py` refuses it above 30 under live. |
| `audit.db_path` | `argus.db` | `argus-live.db` | **A different database file.** See §1.1. |
| `execution.walk_source` | prose | prose | Provenance string. No behaviour. |
| `watchdog.trigger_source` | prose | prose | Provenance string. No behaviour. |

**The risk caps are identical in both files** and have been since 2026-09-11
(C1): `max_concurrent_positions = 1`, `max_loss_per_position = 950.0`,
`daily_max_loss = 950.00`, `max_contracts_per_position = 2`. The welded
invariant holds exactly — `1 × 950 = 950`. **Cert is already running the live
numbers**, which is the point: the cutover does not also change what a trade
costs.

`[server] bind_host` / `bind_port`, `[market]` session windows, `[signal]`,
`[coa]`, the calendar and the blackout path are all identical. Nothing about
*when* Argus runs or *what it picks* changes on cutover day.

Two further changes are **not** config keys and are easy to miss:

- **The credential slot changes.** `trading_credentials(config)` keys on
  `config.environment`, the same authority `is_test` keys the endpoint on — so
  `live` reads `TASTYTRADE_TRADING_LIVE_*` instead of `TASTYTRADE_TRADING_CERT_*`.
  `.env` already holds both worlds and **is not touched by the cutover**; that is
  the whole design.
  → [environment-keyed trading credentials](decisions/2026-09-09-trading-credentials-are-environment-keyed.md)
- **The sandbox 24 h wipe stops.** `sandbox_reset_decision` keys on
  `environment == "sandbox"`, so live never clears `positions`. Correct, and it
  means any residue in the live DB is permanent rather than self-limiting —
  which is why the live DB starts empty and must stay honest.

### 1.1 The live database is a new, empty file — and that is deliberate

`argus-live.db` exists so live can never continue the cert hash chain, and so
cert `positions` rows can never be fed to live reconciliation as expected state
(which would be `missing_at_broker` on day one — a hard halt). That was found as
a **cutover blocker** in the v0.3.17 config-profile regeneration and fixed there.

Consequences of a fresh DB, all expected, none a fault:

- Migrations 0001–0005 apply automatically at the first live boot.
  `Application.startup()` does it; there is no manual step.
- `audit.last_seq` on `/status` restarts near 1. **This is not data loss.** The
  cert chain is intact in `/data/argus.db`, untouched, beside it.
- The durable kill switch reads `CLEAR` — `_read_kill_switch_state` returns
  `CLEAR` when no `kill_switch.trip` row exists. A cert-era trip does **not**
  follow you into live. (Nor does a cert-era resume clear a live trip.)
- The daily-loss breaker has no anchor and takes its first one from the first
  live NLV read. **That is G1's first real exercise.** See §6.
- `positions` is empty, so the process-start reconcile compares the broker's
  entire book against nothing. **A flat account reconciles clean; a single
  foreign position is `unexpected_at_broker` and hard-halts at
  `HaltGrade.ALL`** — which also means the watchdog never starts. That is C8,
  and it is why C8 gates this.

### 1.2 The relative rail's NLV floor is graded, and `950 / 0.03` is not it

**Corrected 2026-09-14.** An earlier draft of the table above read *"implies an NLV
floor of 950 / 0.03 = $31,667 — below it nothing trades."* That arithmetic divides
`max_loss_per_position` by `position_size_pct`, and those two rails are applied to
**different quantities**: the relative rail bounds the *proposal's* defined risk,
not the per-position cap.

`PreTradeGate` (`gate.py:97` (private record, not published)) denies
`size_cap_exceeded` when `proposal.max_loss > position_size_pct × NLV`, and
`proposal.max_loss = contracts × (width − credit) × 100`
(`orchestrator.py:205` (private record, not published),
`signal/models.py:128` (private record, not published)). `size_contracts`
floors at one lot deliberately, so a candidate is always *evaluated* — the gate is
what refuses it. So the binding NLV depends on the **candidate's width and the
credit it collects**, and it degrades in steps rather than switching off:

| NLV at least | What clears the relative rail (at `min_credit = 0.50`) |
|---|---|
| **$15,000** | a 5-wide **1 lot** (risk $450) — the narrowest, cheapest candidate the ladder produces |
| **$30,000** | a 5-wide **2 lots** (risk $900) — the full `max_contracts_per_position` |
| **$31,667** | a 10-wide **1 lot** (risk $950) — the *widest* candidate `signal.max_width` allows |

Every row is the worst case for that shape, because `min_credit` is the smallest
credit the strategy accepts; a richer credit lowers all of them (a 5-wide
collecting 1.00 risks $400, moving the first row to $13,333). **$31,667 is the NLV
at which even the widest
admissible candidate clears — not the NLV below which nothing trades.** Below it,
Argus trades narrower spreads and smaller size; the rail is doing exactly what its
own config comment says it is for, scaling down with the account.

Two consequences for cutover day:

- **P7 is a soft precondition, not a hard one.** Below ~$15,000 nothing trades at
  all and the flip is pointless; between there and ~$30,000 it trades, at reduced
  size, and the day-1 read should expect `size_cap_exceeded` on the wider
  candidates rather than treat it as a defect. `walk_source` in `argus.live.toml`
  states the sizing expectation as "a ~40k account", which clears every row.
- **A 10-wide candidate sits exactly on the absolute rail.** `(10 − 0.50) × 100 =
  950 = max_loss_per_position`, and the gate's test is `>`, so it passes by
  equality at 1 lot and can never reach 2. That is fine, and it is worth knowing
  before reading a `position_max_loss_exceeded` denial as a bug.

---

## 2. The nightly snapshot backed up the wrong database — FIXED 2026-09-11, DEPLOYED 2026-09-13

**Was a blocker; now `scripts/` + `tests/` only, so it deploys with a checkout,
restarts nothing, and is inert for the deploy classifier. It rode `v0.4.1` to the
droplet on 2026-09-13 — see the end of this section for the one confirmation still
owed.**

`scripts/argus-snapshot.sh` opened its source by **literal path**:

```python
src = sqlite3.connect("file:/data/argus.db?mode=ro", uri=True)   # the old line
```

After the flip the process writes `/data/argus-live.db`. The nightly snapshot
would have kept copying the **frozen cert database**, forever, and every
observable downstream of it would have stayed green:

- the copy succeeds (the cert DB is a real, valid SQLite file);
- `verify_chain` passes (the cert chain is intact — it is just finished);
- a fresh `argus-<stamp>.db` lands in `/path/to/backups` every night, so
  `argus-pull-check.sh`'s freshness test (`snapshot < 26 h`) reports `ok`;
- the NAS pulls it on schedule and C4's green streak continues.

**Every layer of the backup chain would have confirmed a backup of the wrong
file.** The live audit log — the only durable record of real orders and real
fills — would have had no off-volume copy at all, and nothing would have said so.
The bug was created by the v0.3.17 fix that gave live its own `db_path`, which is
itself the fix that stops live continuing the cert hash chain: **closing one
cutover blocker opened another, one machine over.**

Same class as v0.3.21's unprivileged-run misreport, the 2026-09-06 NAS pull that
was silently not producing, and `docker compose run` answering a question about a
container it did not create. **A check performed against the wrong subject proves
nothing about the right one.**

### The fix

The backup now resolves its **source** the way the process resolves its
**destination** — `Config.load()` with *no argument*, the same call
`python -m argus` makes, reading the same `ARGUS_ENV` / `ARGUS_CONFIG_DIR` the
container already carries, then `cfg.audit.db_path` (relative; the Dockerfile's
`WORKDIR /data` is what lands it in the state volume).

**No path is constructed anywhere in the script.** A shell-side
`sandbox → argus.db / live → argus-live.db` case would have worked and was
rejected: it is a second authority for which database this host writes, and the
two disagree the first time a profile is added. One loader, one answer.

Three things ride with it:

- **The source is printed every run** — `argus-snapshot: source /data/argus.db
  (profile sandbox, environment sandbox)` — because the failure above was
  invisible precisely in that no surface named its subject.
- **A missing source refuses**, with a message naming the profile and
  `audit.db_path`. That is reachable in exactly one window: the profile flipped
  and the process not yet restarted under it. A backup of something else is the
  original defect one layer on.
- **`tests/test_deploy_wiring.py::test_the_backup_never_names_a_database_by_literal_path`**
  asserts the *absence* of any `.db` literal in the script's instructions, not
  the presence of the loader — so a rewrite that reintroduces the bug in a new
  spelling still has to write a filename down somewhere, and fails on it. Read
  against `_uncommented`, so the comment quoting the old literal in order to ban
  it does not trip its own test.

**Retention was the second half of the question and is answered by procedure, not
by code.** Snapshot names carry a timestamp and nothing else, so after a profile
change the directory transiently holds two different databases under
indistinguishable names, and `$KEEP` live runs rotate the cert ones out. That is
the documented model working — the droplet is a staging window, the NAS is the
archive (runbook §13), and the cert era's nightly snapshots are already on the
NAS. What the filename cannot carry, §4 step 5 does: one final cert snapshot,
copied to `cert-final-<date>.db`, which matches neither the rotation glob nor
`argus-pull.sh`'s. Renaming snapshots per profile was considered and rejected as
a cross-machine change to two globs on a box nothing verifies deployments to.

**DEPLOYED 2026-09-13 with `v0.4.1`, and CONFIRMED WORKING 2026-09-14.** The
deploy landing it and the fix resolving correctly were two different facts; both
are now established, on the Sep 13 and Sep 14 nightly runs. The command, kept
because it is the same one to run after the flip:

```bash
sudo journalctl -u argus-snapshot.service -n 30 --no-pager | grep 'argus-snapshot: source'
```

On cert it reads — and on 2026-09-14 it did, twice:

```
argus-snapshot: source /data/argus.db (profile sandbox, environment sandbox)
```

**That is now the control, and the read is DUE.** The flip happened at 16:30 ET on
2026-09-15, after that evening's 16:30 nightly run had already fired on the cert
database (it did, correctly: `source /data/argus.db (profile sandbox, environment
sandbox)`, 810,552 records). **So the first post-flip run is the 16:30 ET timer on
2026-09-16, and it must name `/data/argus-live.db (profile live, environment
live)`.** If it still names `argus.db`, the backup is on the dead chain and the
live audit log has no off-volume copy. Read it that night, not a week later.

`argus-snapshot.service` carries `EnvironmentFile=/etc/argus/host.env` with no `-`
prefix, so it picks the new profile up on its own — nothing needs changing. Two
things to expect rather than to worry about: the live snapshot will be a few KB
against the cert era's ~590 MB, and because `KEEP` counts **files**, not days, the
14 retained slots become all-live within about two weeks. That is the documented
model (the droplet is a staging window, the NAS is the archive), and
`cert-final-20260915.db` sits outside both globs deliberately.

---

## 3. Preconditions — all of these, before the day

| # | Precondition | State | Confirmed by |
|---|---|---|---|
| — | **ALL PRECONDITIONS WERE MET AND THE FLIP WAS PERFORMED 2026-09-15.** The table below is the record of their state going in | — | — |
| P1 | §2's snapshot fix deployed **and** one nightly run read by eye | **MET 2026-09-14** — deployed with `v0.4.1` on 2026-09-13; Sep 13 and Sep 14 runs both printed the cert source line and verified an intact chain | `journalctl -u argus-snapshot.service -n 30` prints `argus-snapshot: source ...` naming the expected database |
| P2 | **C8** — the tastytrade account holds nothing Argus did not place, and no working orders | **MET 2026-09-14** — the last non-Argus position closed under normal trade management, not forced. **Re-confirm the morning of day 1**: this is a standing property, not a one-time tick | my.tastytrade.com, by eye |
| P3 | **G1** — accepted in writing that day 1 live is the daily-loss breaker's first real exercise, with a manual P&L cross-check planned (§6) | **MET — accepted in writing 2026-09-14.** Day 1 (2026-09-16) is an FOMC blackout, so the acceptance stands but its first real exercise slips to the first day Argus holds a position — see §6 | this file, and go-live-findings G1 |
| P4 | **C4** — `argus-pull-check.timer` has reported `ok` on several consecutive days | **MET 2026-09-14** — eight consecutive `ok` days, Sep 07 → Sep 14, both arms green; no failed units; both timers armed | droplet read |
| P5 | **C6** — CI green on `main`, and the tag to be deployed is already running in cert and has run at least one clean session | **MET 2026-09-15** — the flip was performed on `v0.4.2`, which was already deployed in cert and had run that day's cert session (4 trade ideas, 1 filled, held to expiry) | Actions + `/status` |
| P6 | **C5** — **the broker will not take a closing order** (broadened 2026-09-14 from "a stuck `Cancel Requested`") is accepted as first-week risk, with a monitoring plan and a manual-close SOP | **MET — accepted in writing 2026-09-14** | written down, go-live-findings C5 |
| P7 | NLV in the live account supports the size you expect to trade — **a graded floor, not a single number (§1.2)**: ~$15,000 for one 5-wide lot, ~$30,000 for two, ~$31,667 for the widest candidate | **MET** — the account is funded above every row | account balance |
| P8 | The blackout list still runs past the flip + 30 days | should hold — the list runs to `2026-12-24` | `/status` → `event_blackout.days_remaining` |
| P9 | `credcheck` clean on all three slots, **in the running container** | **MET 2026-09-15** — re-run as pre-flight via `docker exec`, all three slots present and complete, no live slot carrying a cert value | §4 |

**The cutover moves one variable.** If a release is also due, deploy it in cert,
let it run a session, verify, and flip on a *later* day. A combined
release-plus-cutover has two candidate causes for every symptom, and the whole
value of the `host.env` mechanism is that the flip has exactly one.

> **Tag state, 2026-09-14.** The repo's newest tag is `v0.4.1`, tagged and
> deployed to the droplet 2026-09-13 (~10:21 ET). `v0.4.1` is config + docs +
> `scripts/` with **zero `src/` diff** against `v0.4.0`, so the running image is
> `v0.4.0`'s and the flip sits on top of a `src/` tree that has been in cert since
> 2026-09-10. **Confirm `/status` reports `v0.4.1` and that it has run at least
> one clean cert session before flipping** — the earlier note here recorded a
> `v0.4.0` / `v0.3.22` mismatch that no longer exists.

---

## 4. T-minus: the pre-flight, on the droplet

Run this the day before. Nothing here changes anything.

```bash
cd /opt/argus
PROFILE="$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)"; echo "$PROFILE"
test -f "argus.$PROFILE.toml" && echo "profile: $PROFILE" || echo "STOP"
```

**1. The live config file is present, pinned, and is the one the tag carries.**

```bash
git describe --tags                                   # the deployed tag
git status --short                                    # must be clean
grep -n 'account_number' argus.live.toml              # -> "5XX00000"
grep -n '^environment' argus.live.toml                # -> "live"
grep -n 'db_path' argus.live.toml                     # -> "argus-live.db"
sha256sum argus.live.toml | cut -c1-12                # the sha to expect after the restart
```

**2. `./.env` carries nothing that would override the profile or pin an old
image.** Three variables, one command:

```bash
grep -nE 'ARGUS_CONFIG|ARGUS_ENV|ARGUS_IMAGE_TAG' /opt/argus/.env   # must print NOTHING
```

`ARGUS_CONFIG` wins over the profile (`config.resolve_config_path`), `ARGUS_ENV`
here would be a second, hand-edited answer to a question `host.env` already
answers, and a stale `ARGUS_IMAGE_TAG` makes the restart come up on an old image
while every other assertion passes. `env_file` entries reach the container, so
none of these is inert.

**3. Credentials, in the RUNNING container.** `docker exec`, never
`docker compose run` — a `compose run` reads `env_file` fresh into a **new**
container and says nothing about the environment the running one was created
with:

```bash
sudo docker exec argus python -m argus.tools.credcheck
```

Expect all three slots present and complete, and *"no live slot is carrying a
cert value."*

**`credcheck` reports SHAPE, never authentication** — its own output says so.
Two values that are well-formed but come from different app registrations is
precisely the failure it cannot see, and the three trading tools each
`raise SystemExit` under `environment == "live"`, so **the first live
`/oauth/token` call happens inside the cutover itself.** Nothing needs building
for this; knowing it in advance is the whole mitigation, and the rollback is the
same one line.

**4. Read the divergence, since the deploy's own diff cannot show it.** On an
ordinary config release `--dry-run`'s `git diff` between tags *is* the review.
Here both configs are at the same tag, so that diff is empty and useless. Review
the real change instead:

```bash
diff <(grep -vE '^\s*#|^\s*$' argus.sandbox.toml) \
     <(grep -vE '^\s*#|^\s*$' argus.live.toml)
```

Expect exactly the eight rows of §1 and nothing else. **A ninth is a stop.**

> **The `[risk]` comment block in both files is stale prose, and you will read it
> here.** Both `argus.sandbox.toml` and `argus.live.toml` still describe
> `position_size_pct` as *"2% of NLV"*, `daily_max_loss` as *"RAISED 1740 → 2500"*
> and *"exactly 1250 × 2"*, and `max_concurrent_positions` as *"SET TO 2 BY
> RICHARD"* — all of it written before v0.4.1 (C1) synced both profiles to
> `1 × 950`. **The VALUES are correct and identical in both files; only the
> comments lag.** The `grep -vE '^\s*#'` above strips them, which is why the
> divergence check is unaffected.
>
> **Do not fix them before the flip.** These files are bind-mounted, so a
> comments-only edit changes the sha, makes a config release out of a typo fix,
> and gives cutover day a second moving variable — the one thing the `host.env`
> mechanism exists to prevent. Correct them in a config-only release afterwards,
> on their own day.

**5. Take the cert era's final snapshot, and move it OUT of the rotation glob.**
This is your reference point for "the cert record was intact when we left it",
and it is the only copy of that distinction that survives on this box:

```bash
sudo ARGUS_ENV="$PROFILE" /opt/argus/scripts/argus-snapshot.sh
ls -lt /path/to/backups/ | head -3
```

Confirm the line naming the source — it must be the **cert** database:

```
argus-snapshot: source /data/argus.db (profile sandbox, environment sandbox)
```

Then take it out of rotation. Snapshot filenames carry a timestamp and nothing
else, so once live runs start, `$KEEP` nightly runs of the *live* database rotate
the cert ones out and no filename distinguishes the two chains:

```bash
sudo cp /path/to/backups/"$(cd /path/to/backups && ls -1t argus-*.db | head -1)" \
        /path/to/backups/cert-final-$(date +%Y%m%d).db
ls -l /path/to/backups/cert-final-*.db
```

`cert-final-*.db` matches neither the rotation glob nor `argus-pull.sh`'s
`argus-*.db`, so it stays put and is **not** pulled to the NAS — copy it off by
hand if you want it there. The cert era's nightly snapshots are already archived
on the NAS (50 of them); this is the one you can find without going to look.

Run the snapshot **once**. `KEEP` counts files, not days, so repeated hand-runs
rotate real days off the back.

---

## 5. Cutover day

### 5.1 Timing — after the close, never before an open

**Flip after 16:15 ET on a trading day, or any time on a non-trading day.** Not
in the morning before the open.

This is §16.1's reasoning, and it is stronger here. A post-close flip buys ~17
hours before the next 09:40, and — because the supervisor idles outside its
session window — **the first live boot logs in, reconciles against the live
account, and then does nothing.** That is a complete proof of the OAuth
registration, the account pin, and a clean reconcile, with **no opportunity to
trade** between the proof and your decision to keep it or revert. There is no
cheaper rehearsal available, and there is no way to rehearse the live boot
without performing it.

Do not flip with a position open anywhere, in either world. §7 explains why.

### 5.2 The flip

```bash
sudo cat /etc/argus/host.env                              # expect: ARGUS_ENV=sandbox
printf 'ARGUS_ENV=live\n' | sudo tee /etc/argus/host.env >/dev/null
sudo chmod 600 /etc/argus/host.env
sudo cat /etc/argus/host.env                              # expect: ARGUS_ENV=live
```

**One line. No quotes.** systemd accepts `ARGUS_ENV="live"` and `argus-deploy.sh`
strips them, but the `sed` one-liner used throughout the runbook and in
`docker-compose.yml`'s own comment does not — it hands back `"live"` with the
quotes attached and sends compose looking for `argus."live".toml`.

Nothing has changed yet: the running process read its config at startup and there
is no hot-reload, by design.

### 5.3 The restart — through the deploy script, not `systemctl`

```bash
cd /opt/argus
scripts/argus-deploy.sh v<CURRENT-TAG>        # the tag ALREADY deployed. No sudo on the script.
```

Re-deploying the tag already checked out restarts unconditionally, and that path
gives you three assertions a bare `systemctl restart` gives you none of: the
script **resolves the profile from `host.env`** (so it is now reading `live`),
**refuses if the tag does not carry `argus.live.toml`**, and **reads the config
fingerprint back off the startup line after the restart** — comparing the
profile, the filename *and* the sha. That read-back is the only post-restart
assertion this release class has.

**Two things in the plan output will look wrong and are not:**

- `build: no` — correct. The image is profile-agnostic and did not move.
- `config: no (unchanged since v<CURRENT-TAG>)` — **true about git and
  misleading today.** It answers "did this file's bytes change between tags?",
  and today's change is *which file is mounted at all*. The line that matters is
  the one above it:

  ```
  profile: live (argus.live.toml, sha <12 hex>) — from /etc/argus/host.env
  ```

  and the sha must be the one §4 step 1 printed.

Expect, after the restart:

```
argus-deploy: running config reports: live/argus.live.toml @<sha>
```

A `!! RUNNING CONFIG ... EXPECTED ... !!` is a stop, and §7 is one line away.

### 5.4 Read the boot yourself — the script's read-backs cannot see a crash loop

This is an open finding in its own right: `argus-deploy.sh` asserts by grepping
the journal, so a process that dies *after* the startup banner writes that banner
again every cycle and both read-backs pass, while `restart: unless-stopped` keeps
compose "active". The script's `RED_FLAGS` grep catches `Traceback` and
`refusing to start` — which does cover an unhandled `MissingCredentialsError`,
the most likely live-specific failure — but not the class.

So look at it directly, and use an assertion a dead process cannot satisfy:

```bash
sudo journalctl -u argus.service -n 80 --no-pager
```

The first line of the journal is the one that carries both halves:

```
INFO argus.app argus build v<X.Y.Z> config live/argus.live.toml @<sha>
INFO argus.db.lock acquired write lock on /data/argus-live.db
INFO argus.control kill-switch control server listening on 192.0.2.10:8443 (POST /kill)
INFO argus.app argus started in live mode
```

`/data/argus-live.db` on the lock line is the single best confirmation that the
live profile actually took: it is derived from the config the process loaded, and
it is a *different file name*, not a different value of the same one.

Then the liveness check the journal cannot give you — **a `GET /status` a few
seconds later**, which a dead process cannot answer:

```bash
curl -fsS -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
  http://192.0.2.10:8443/status | jq '{environment, version, config, credentials, account, halted, kill_switch, daily_loss_breaker, event_blackout}'
```

Expect:

| Field | Expected |
|---|---|
| `environment` | `"live"` |
| `config.profile` / `config.file` | `"live"` / `"argus.live.toml"` |
| `config.sha` | the sha from §4 step 1 |
| `credentials.trading` | `TASTYTRADE_TRADING_LIVE_*` |
| `account.pinned` / `account.number` | `true` / `"5XX00000"` (the **Account** row reads `5XX00000 — pinned`) |
| `account.nlv` | **`null` at an idle post-close boot, and that is correct** — see the note below |
| `halted.halted` | `false` |
| `kill_switch.engaged` | `false` |
| `session.phase` | `idle`, with `next_session_open` = the next trading day 09:40 ET |

**CORRECTED 2026-09-15, on performing this step.** An earlier draft of the table
above read *"`account.nlv` — the real balance, **not** `100000.0` — cert's frozen
figure"*, and called it *"the field that proves you are looking at the real
broker."* **It cannot be, at the moment §5.1 tells you to look.** `account_nlv` is
read off the `RiskSnapshot` (`status.py:1884` (private record, not published)),
which is built inside a session — and a post-close flip comes up at
`session.phase: idle`, where `account`, `market_data`, `account_data`, `watchdog`
and `unmanaged` are all legitimately `null`. The two expectations contradicted each
other: §5.1 mandates the idle boot precisely so there is no opportunity to trade.

**What proves you are looking at the real broker at an idle boot is the clean
reconcile**, because `startup_reconcile` opens a real trading session to perform it
— so it cannot come back `ok` without a successful live `/oauth/token` and two
answered REST calls against the pinned account. Read `broker reconciliation clean`
in the journal and the `reconcile.result` row on `/status`, not `nlv`.

**The NLV read is a DAY-1 check, not a cutover-day one.** Cert reports a frozen
`100000.0` and `daily_pnl: 0.0` forever; a real marked NLV is the first evidence in
this system's history that the daily-loss breaker has an input at all — and it first
appears once the session is up, at ~09:45 ET. See §6.

Also confirm the reconcile came back clean, in the journal:
`broker reconciliation clean`. **A halt here is the rail working**, not a
cutover failure — it means the account is not flat (C8) and you should revert per
§7 and resolve the position, not override anything.

### 5.5 Prove the kill switch on the live process — DONE 2026-09-15

Before you walk away. Use the **actual phone Shortcut**, not curl — the point is
to prove the one-tap mechanism you would reach for tomorrow, on today's process:

Expect `200 {"status":"tripped"}`, then `/status` showing
`kill_switch.engaged: true`.

**Clear it with `POST /resume?confirm=yes`, not a restart.** The kill switch is
durable since v0.3.14 — it is rehydrated from the audit log with no date bound,
and a restart restores it rather than clearing it. `/resume` is the only thing
that clears it anywhere in the codebase.

```bash
curl -fsS -X POST -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
  'http://192.0.2.10:8443/resume?confirm=yes'
curl -fsS -H "Authorization: Bearer $ARGUS_KILL_TOKEN" \
  http://192.0.2.10:8443/status | jq '.kill_switch'
```

Expect `engaged: false`. **Do not leave the switch engaged overnight** unless you
mean to — it now survives the night, which is exactly the property it was given.

Then leave it to idle.

**DONE 2026-09-15.** `kill_switch.trip` at 16:45:13 ET, `kill_switch.resume` at
16:45:37 ET, both Tier 1 on the live chain (`audit.last_seq: 5`). Cleared with
`POST /resume?confirm=yes`; `/status` then read `engaged: false`, `restored: false`,
`halted.halted: false` with `grade: null`, and the resume payload carried
`restart_required: false`. **The HTTP kill path engages the switch without writing a
`HaltRecord`** — so the post-resume state is genuinely clean rather than "switch
clear, latch still set." That was the first exercise of the durable switch on live
data and it left no residue.

---

## 6. Day 1 — the first live session

> **DAY 1 IS 2026-09-16, AND IT IS A KNOWN FOMC BLACKOUT DAY. ARGUS WILL PLACE NO
> ENTRIES.** `blackout_dates.txt` carries the date, `/status` confirms the list is
> loaded (18 dates, 100 days of runway), and
> `strategy.py` (private record, not published) returns `EVENT_BLACKOUT` for
> every candidate. `compute_regime` never returns `None` and never leaves `today`
> unset, precisely so this check cannot be skipped by an estimator failure — so
> there is no path around it.
>
> **What that still exercises:** the whole session lifecycle against the real
> broker — the scheduler, the session-path login (a *different* open from the
> process-start one proven at the cutover), the reconcile at ~09:40, market data,
> the chain, the watchdog loop, and the close. All of it with zero order risk.
>
> **What it does NOT exercise, and this is the part to get right:** **G1's day-1
> check is deferred, not satisfied.** The check that matters is that `daily_pnl` is
> **not identically zero** — and on a flat account with no entries and no cash
> movement, NLV does not move, so `daily_pnl` is *legitimately* exactly `0.0`.
> That is pixel-identical to cert's structural zero, which is the exact failure
> mode G1 exists to detect. **A zero on 2026-09-16 proves nothing in either
> direction.** The real G1 read is the first day Argus actually holds a position.
>
> **The one G1 input that IS readable tomorrow:** `account.nlv` should render the
> real balance rather than cert's frozen `100000.0`, once the session comes
> up and builds its first `RiskSnapshot` — read it at ~09:45 ET. That confirms the
> breaker's input is *readable*; the delta half waits.
>
> Everything below describes a day on which Argus trades. Apply it to the first
> non-blackout day.

Watch this one actively. It is the first session in the system's history where
`environment = "live"`, and **G1 makes it the daily-loss breaker's first real
exercise.**

**Before the open:** confirm the account is still flat and has no working orders.
One foreign position hard-halts the session reconcile. **C8 being met on
2026-09-14 is not a permanent fact** — it is a property of the account that has to
hold every morning, and the only thing keeping it true is that nothing else trades
there any more.

**At ~09:40 ET**, `phase` should walk `idle → logging_in → reconciling →
running`. `logging_in` persisting past ~10:30 means all six login attempts failed.

**Through the day, the G1 cross-check.** This is the whole reason day 1 is
watched rather than trusted:

- `account.nlv` must **move**. A frozen NLV in live means the breaker's input is
  dead and the rail is as inert as it is in cert — with real money behind it.
- Check `account.daily_pnl_anchored` is `true` first (v0.4.4+). If it is `false`,
  `daily_pnl` reads `null`: the day has no P&L anchor and the breaker is reading a
  placeholder 0.0 — treat that like a frozen NLV.
- Cross-check `account.daily_pnl` against the tastytrade UI's own day P&L by
  hand, at least twice: once mid-morning and once after the entry window closes.
  `DailyPnl` is whole-account NLV minus a whole-account anchor; if the two
  disagree, **the rail is measuring the wrong quantity** and the day should be
  halted with `POST /kill` while you work out why.
- **No cash may move into or out of the account on a trading day.** `DailyPnl`
  reads a transfer as P&L, and a same-day restart re-uses the day's *original*
  anchor — the contamination is not restart-clearable.

**The halt control during the session is `POST /kill`, not the cutover
rollback.** The switch stops entries immediately, including an entry order
already working at the exchange, while the watchdog, the virtual stops and the
15:30 `FLATTEN_BEFORE_CLOSE` all keep running. Reverting `host.env` mid-session
does the opposite and is described in §7.

**After the close**, the read-only reads (`tools.desk`, `tools.evaluability`)
work as they always have, against `argus-live.db` now — the preamble resolves
`--config /etc/argus/argus.$PROFILE.toml` and `$PROFILE` is `live`.

---

## 7. Rollback

**The rollback is the same one line, and it takes effect on a restart:**

```bash
printf 'ARGUS_ENV=sandbox\n' | sudo tee /etc/argus/host.env >/dev/null
sudo chmod 600 /etc/argus/host.env
cd /opt/argus && scripts/argus-deploy.sh v<CURRENT-TAG>
```

Verify the same way: `config: sandbox/argus.sandbox.toml @<sha>` on the startup
line, `/data/argus.db` on the lock line, `environment: "sandbox"` on `/status`.

### The one hard precondition

> **NEVER roll back while a live position is open.**
>
> Under `sandbox` the process authenticates to **cert**, with cert credentials,
> against a **different account**, and reads `/data/argus.db` — which contains no
> row for the live position. The live spread then has no watchdog, no virtual
> stop, no mark, no `/status` row and no 15:30 flatten. On 0DTE it rides to
> settlement, and the loss is whatever settlement makes it, up to the width.
> Nothing in the system will report this, because from cert's point of view
> nothing is wrong.
>
> If something is wrong mid-session: **`POST /kill`**. It stops entries and keeps
> the watchdog. Roll back only once the book is flat — after the close, or after
> the flatten has done its job.

### What the rollback does and does not undo

| | |
|---|---|
| **Undone** | the endpoint, the credential slot, the account pin, the risk rail values, the staleness bound, the database being written |
| **Not undone** | anything that already traded. Positions opened live are at the live broker and are real. |
| **Not undone** | `argus-live.db`. It stays on the volume with its rows, and is picked straight back up if you flip forward again — including any durable kill-switch trip, which has no date bound. |
| **Not touched** | `.env`. Both worlds live in it at once and neither direction of the cutover edits it. That symmetry is the point. |
| **Not touched** | `/data/argus.db`. The cert chain is untouched by the live era and resumes where it left off. |

A same-evening rollback, performed flat, costs nothing but the evening.

---

## 8. After the flip — what changes in day-to-day operation

**In effect since 2026-09-15.** Everything in this section is now the present
tense, not a forecast.

- **`$PROFILE` is now `live` everywhere**, including the README preamble, the
  `tools.*` `--config` path, and every `sudo ARGUS_ENV="$PROFILE" docker compose`
  invocation. The preamble reads it from `host.env`, so nothing needs editing —
  but anything you have in shell history with a literal `sandbox` is now wrong.
- **The cert-era record becomes harder to read, and this is worth knowing before
  you want it.** Compose mounts `./argus.${ARGUS_ENV}.toml` and *only* that, so
  after the flip `argus.sandbox.toml` is not inside the container and
  `tools.desk --config /etc/argus/argus.sandbox.toml` cannot find it. To read the
  cert history, mount it explicitly for that one run:
  ```bash
  sudo ARGUS_ENV=live docker compose -f /opt/argus/docker-compose.yml run --rm \
    -v /opt/argus/argus.sandbox.toml:/etc/argus/argus.sandbox.toml:ro argus \
    python -m argus.tools.desk --config /etc/argus/argus.sandbox.toml
  ```
- **The 24 h sandbox reset stops.** Nothing clears `positions` any more; every
  row there is now a fact about a real account.
- **The daily-loss breaker is live and untested.** Treat its first trip — if it
  comes — as an event to read carefully rather than as a routine rail firing.
- **`unmanaged_order_cancel` was `false` in live at the cutover and is `true`
  since v0.4.5 (deployed 2026-09-20).** P5's recheck now re-sends the cancel of an opening
  order the broker confirms is still working. `/status`'s `unmanaged` section
  names the mode only while a residual is outstanding (`auto-cancel ON for opening
  orders`); with none it reads `none working`, and the config sha is the read-back.
  → [decision](decisions/2026-09-19-live-cancel-recheck-promotion.md)
- **One config-only cleanup is owed, on its own day, not urgent:** the stale
  `[risk]` comment blocks in both profiles (§4 step 4). The other one listed here,
  promoting `watchdog.unmanaged_order_cancel` in live, shipped in v0.4.5.
- **Both `walk_source` and `trigger_source` are still the 2026-08-10 values.**
  They are carried into live deliberately, not by omission, and the owed
  data-backed pass is gated on the **first size increase**, not on this flip.
  `entry_drift_abort_points` is the one parameter whose data is already
  admissible; that computation is still owed.

---

## 9. What cannot be verified before the day

Stated plainly, in the spirit of §16.12. None of these is a reason to delay; each
is a reason to watch.

1. ~~**That the live app registration authenticates.**~~ **CLOSED 2026-09-15 by
   doing it.** `startup_reconcile` opens a real trading session, so the clean
   reconcile at 16:33:18 ET required a successful live `/oauth/token` against the
   production API. `credcheck` reports shape, never authentication, and the trading
   tools refuse to run under `live` — so this could only ever have been settled
   inside §5.3, which is what the post-close timing and the one-line revert were
   for.
2. ~~**That the live account reconciles clean.**~~ **CLOSED 2026-09-15** — `ok:
   true`, no position discrepancies, no order discrepancies, no unmappable orders.
   That is also C8 confirmed by machine. A halt here would have been the rail
   working; it did not halt.
3. **The daily-loss breaker end to end.** G1. Structurally unexercisable outside
   live; day 1 is its first real exercise and §6 is its only cross-check.
4. **Real fills, real slippage, real multi-leg matching.** Cert's matching for
   multi-leg SPXW orders is undocumented, and the entry path has been effectively
   stopped in cert since 2026-09-02 by the lost 5-point strike ladder. **Every
   entry-side number measured since then describes cert's instrument table, not
   Argus.** Live-min-size is the first stage with genuine fills.
5. **A stuck `Cancel Requested`.** Still never observed (C5, accepted risk). The
   benign fast case is proven; the stuck one is not.
6. **An UNWATCHABLE position gets no flatten.** `/status` names it, but
   `ExitExecutor.close` needs the `OpenSpread` that could not be built. On 0DTE it
   rides to settlement. Known gap, carried into live deliberately.
7. **Residual orders do not rehydrate across a restart.** P5 prevents the state
   from arising; it does not resolve a previous process's orphan.

---

## References

- [`go-live-findings.md`](go-live-findings.md) — G1, G2 and the pre-flip checklist
- `argus-deployment-runbook.md` §8 (private record, not published) — `host.env`, the credential grid, the account pin
- `argus-deployment-runbook.md` §17 (private record, not published) — build, deploy, rollback, the two tags
- [config profiles](decisions/2026-09-04-config-profiles.md) — the mechanism, and the `db_path` cutover blocker it found
- [environment-keyed trading credentials](decisions/2026-09-09-trading-credentials-are-environment-keyed.md) — why the cutover no longer touches `.env`
- [pinning the traded account](decisions/2026-09-10-pinning-the-traded-account.md) — `[broker] account_number`
- [the live account is not dedicated](decisions/2026-08-31-live-account-is-not-dedicated.md) — why C8 gates everything
- [kill-switch persistence](decisions/2026-09-01-kill-switch-persistence.md) — why `/resume`, not a restart
- [halt scope split](decisions/2026-08-29-halt-scope-split.md) — what a halt takes down, and what it leaves running
