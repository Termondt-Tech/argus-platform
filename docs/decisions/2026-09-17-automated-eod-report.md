# The EOD report runs itself (2026-09-17)

*Since the cert era the end-of-day read has been four commands typed by hand at
~16:30 ET, with the output pasted into a `.txt` file in the repo. That worked
because nothing was at stake and because a day missed cost a day of telemetry. In
live it is the operator's only daily look at a real account, it grew a step
(**G1** — the daily-loss breaker's input) that **cannot be read from the surface
you would naturally read it from**, and the failure mode of a manual SOP is
silence: a skipped evening looks exactly like a quiet one.*

> **DESIGNED AND BUILT 2026-09-17.** Four choices were put to Richard and all four
> took the recommendation: **Slack digest + NAS archive** for delivery; **droplet +
> NAS for transcripts, curated log in git**; **thin shell + `tools.eod`** for
> structure; **`gate.decision` rows plus one `/status` sample** for the G1 numbers.
> Everything below follows from those four, and everything below is as built except
> where marked **AS BUILT**. **Not yet installed on the droplet** — it is an
> ancillary unit, so the deploy does not install it, and the rollout (§10) runs it
> beside the manual SOP for a week before the timer is enabled. **Ships in v0.4.4**
> (`tools.eod` is `src/`, so it rides that release's image build).
>
> Shipped: `src/argus/tools/eod.py`, `scripts/argus-eod.sh`,
> `argus-eod.{service,timer}`, `tests/test_eod_report.py` (19), four wiring tests in
> `tests/test_deploy_wiring.py`, the ancillary-unit arm in `argus-deploy.sh`, a
> `README.md` §4 section, and `ARGUS_REPORT_SLACK_WEBHOOK_URL` in `.env.example`.
> **One thing was extracted rather than added: `desk.summarize()`** — see §3.
>
> Three build-time decisions worth carrying, none of which changes the design:
>
> - **The Slack message BODY is rendered in `tools.eod`, not in the shell**
>   (`slack_text`, written as `argus-eod-<date>.slack.json`, POSTed with
>   `curl --data-binary @file`). The design said the shell reads the JSON digest
>   with `jq` — but `jq` is not guaranteed on the host, and hand-escaping JSON in
>   bash is its own class of bug. This is the same rule one step further: the shell
>   now makes NO decisions and escapes NOTHING.
> - **A deliberate skip is not a failure.** `--no-status` (the by-hand path) renders
>   the rails section as *"not read … UNVERIFIED"*; a `/status` read that was
>   ATTEMPTED and failed is still a failed section. Rendering a skip in red would
>   teach the reader that a red section on this page is normal, which is the same
>   defect as a digest nobody reads.
> - **`RuntimeError` carries no traceback into the transcript.** It is the type this
>   module raises for its own refusals (no `/status` document, no journal file), and
>   a stack trace under an expected state makes it read as a crash.
>
> **REVIEWED 2026-09-18, before shipping in v0.4.4. Four defects fixed, none of which
> changes the four choices above:**
>
> - **A `Persistent=true` catch-up reported the WRONG DAY.** The shell passed
>   `--date $(date +%F)`, so a run missed across a reboot and caught up the next
>   morning rendered *that* morning's pre-session day (with `--from-feed` refusing
>   before 16:15), and the missed day got nothing — the timer's own comment ("a late
>   one is as true as a punctual one") was false as built. **An undated run now
>   resolves the latest SETTLED session** (`MarketCalendar.is_settled`, half-day
>   aware) **and renders a day once**: with `--out-dir`, a day that already has a
>   transcript is skipped, so an evening catch-up can never overwrite the punctual
>   report whose `--from-feed` scoring it cannot reproduce. The shell stopped
>   choosing a date at all; it passes `--date` only for `ARGUS_EOD_DATE` (the
>   re-render path, which always renders), and delivers the file its own run wrote
>   (`-newer` a stamp), never a guessed name. The journal window is the named day,
>   or the last 36 h.
> - **A settled hold paged as `NOT FLAT — N contract(s) OPEN` in red.** At 16:45 a
>   0DTE spread nothing closed has EXPIRED — the flatten's modal regime makes that
>   an ordinary evening, and a red "OPEN" on ordinary evenings trains the reader to
>   skip the headline. After settlement it reads `:warning: HELD TO EXPIRY` and says
>   to check the settlement against the broker (ITM is a cash loss the ledger does
>   not show yet). Before settlement, and whenever a position is UNPRICEABLE
>   (`FLAT STATE UNREADABLE`), it is still red.
> - **A failed Slack POST exited 0.** The comment said `--fail` makes a rejected
>   webhook a failed unit; the `||` swallowed it. It now pages on the alert webhook
>   AND fails the unit (after retention runs).
> - **The Slack line named the container's `/reports/...` path**, which does not
>   exist on the droplet. `--host-report-dir` supplies the host path.
>
> **And one incident the build caused:** `tools.eod.main()` calls `load_env()`, which
> writes the repo's `.env` into `os.environ` outside monkeypatch — so the two tests
> that drive `main()` leaked the developer's REAL alert webhook into the rest of the
> pytest process, and every startup-based test after them posted to Slack. No earlier
> tool test called a `main()`, which is why it never fired before. Fixed for the
> whole suite in `tests/conftest.py` (`_no_real_env`: `find_dotenv` blinded, both
> webhook variables cleared), pinned by a test, and the EOD tests stub `tools.mark`
> so no test is one missing event loop away from the live feed.
>
> **Known and left:** `desk.load`/`read_g1` run OUTSIDE the per-section catch, so a
> defect there fails the whole job (loud — the ERR trap pages) rather than one
> section; §8 reports the snapshot's age, not whether its `verify_chain` passed
> (read `systemctl status argus-snapshot` for that); and `tools.mark`'s
> `configure_logging` binds the root handler to the captured buffer, so log lines
> from later sections in the same run are dropped rather than transcribed.

## What this is, in one sentence

A host-side systemd timer fires at 16:45 ET on trading days, runs one read-only
container that renders the day's report from the live audit chain, writes the
transcript beside the nightly snapshots where the NAS already collects them, and
posts a digest to Slack — and if it cannot do that, it says so loudly rather than
quietly not happening.

---

## What the existing machinery decides for us

Four of these are constraints discovered by reading the tree, not preferences.
They are the reason this design has the shape it has.

**1. There is a precedent and it is exact.** `argus-snapshot.{service,timer}` +
`scripts/argus-snapshot.sh` is this job with a different payload: a host timer at
16:30 ET, `EnvironmentFile=/etc/argus/host.env` with **no `-` prefix** so a missing
profile fails the UNIT, `docker compose run --rm` into the same image, read-only,
`Persistent=true`. `scripts/argus-pull-check.sh` is the second precedent and it
already carries a tested Slack `notify()` that reads `ARGUS_SLACK_WEBHOOK_URL` out
of `/opt/argus/.env` and treats a missing webhook as a missing channel rather than
a failure. **Copy both; invent neither.**

**2. An ancillary unit is INERT to the deploy and that is a trap with a name.**
`argus-deploy.sh` classifies `argus-snapshot.*` and `argus-pull-check.*` as
ancillary: changing one does not restart the trading process, and the deploy
**does not install it** — it shouts `ancillary units CHANGED` and leaves it to a
human. `tests/test_deploy_wiring.py` pins that list. A new `argus-eod.*` pair that
is not added to both deploys green, passes every read-back, and is never installed
on the box. That is the silent-reattach failure one unit over, which this repo has
already paid for once.

**3. `/status` CLEARS the risk source at session end, so the G1 numbers are not
there at 16:45.** From `status.py`: *"On session end the risk source is CLEARED, so
`/status` reports 'no active session' rather than an ever-growing age from a torn
down feed."* A report that curls `/status` after the close gets `phase: ended` and
**no `account.nlv`, no `daily_pnl`** — the two numbers the live cutover runbook §6
makes the day's whole cross-check. An automated EOD built on the endpoint would
have rendered them blank or, worse, absent-as-zero, on exactly the rail that cannot
be validated any other way.

**4. They ARE durable in the chain.** `gate.decision` is Tier 1 and its payload
carries the `RiskSnapshot`, which holds `account_nlv` and `daily_pnl`. So the day's
NLV/P&L **path** is recoverable from the audit log — strictly better evidence than
one spot read, because G1's question is *"does it move?"*, and re-renderable for a
past date. **On a day with no candidates there are no gate rows and therefore no
series**, and the report must say *"no series"* rather than `0.0`: a structural
zero rendered as a number is the precise failure G1 exists to detect.

**5. `--from-feed` is wrong on a half-day.** It reads the *current* SPX quote as
the close; a half-day settles at 13:00, so a 16:45 read is three and a half hours
of drift presented as a settlement price. A human running the SOP knows what day it
is. An unattended job must ask `MarketCalendar`.

**6. Everything in the report is read-only, and must stay that way.** All the
tools open `mode=ro` and take no lock; `tools.mark --dry-run` writes nothing. The
write-mode mark needs the service stopped — so **the automated job must never run
one**, and must never stop the service. That is stated again as a rail in §8.

---

## The shape

### Units and timing

| | |
|---|---|
| `argus-eod.timer` | `OnCalendar=*-*-* 16:45:00` (host TZ is Eastern), `Persistent=true` |
| `argus-eod.service` | `Type=oneshot`, root, `EnvironmentFile=/etc/argus/host.env` (no `-`), `ExecStart=/opt/argus/scripts/argus-eod.sh` |

**16:45 ET is chosen, not arbitrary.** It is after `--from-feed`'s 16:15 floor and
after the 16:30 snapshot, so the snapshot's *own* result is a section of the report
rather than something you check separately tomorrow.

**The trading-day guard lives in the job, not in `OnCalendar`.** The timer fires
every day; the job asks `MarketCalendar` whether today was a session and exits 0
silently if not. That keeps `Persistent=true` safe (a catch-up run on a Saturday
after a reboot does nothing) and keeps one authority for "was today a trading
day?" — the same one the scheduler uses.

### The split, and the one rule that forces it

`scripts/argus-eod.sh` owns the profile guard, the trading-day gate's exit code,
file naming, retention and delivery. `python -m argus.tools.eod` owns **content**:
it runs every section in one container start and emits two artifacts — a human
transcript (`.txt`) and a machine digest (`.json`).

**The shell must never grep the tools' prose to build the Slack message.** That is
the `reject_reason` rule applied to our own output: branch on the mechanism, never
on a message another program is free to reword. A digest assembled by `grep -A3
'REALIZED'` is a monitoring surface that goes quietly wrong the day someone
improves a heading — and it fails in the reassuring direction, printing a real
number from the wrong line. So the digest is computed from **values** inside
`tools.eod` and handed to the shell as JSON, which `jq` reads.

**AS BUILT: the day's arithmetic was EXTRACTED, not re-derived.** The digest needs
realized, unrealized, fees, held contracts and the spent-exit list, and every one of
those was computed inside `desk._report`'s printing loop. Re-deriving them in
`tools.eod` would have made the digest a **second scorer of the day** — the hazard
`desk.py`'s own docstring names between `desk` and `mark`, one layer down and far
worse, because `desk` and `mark` are at least compared by a human on the same page
while a Slack line and a transcript are compared by nothing. So the classification
loop moved out into `desk.summarize() -> DeskSummary` (pure, no I/O), `_report` now
formats from it, and `tools.eod` reads the same object. One computation, two
renderings. `DeskSummary.flat` is fail-closed and refuses the claim when any
execution is UNPRICEABLE: "no fill prices were reported" is not "nothing is held".

The cost of that split is real and worth stating: `tools.eod` lives in the image,
so a change to report *content* ships as a release and restarts the trading
process. Report *plumbing* (timing, retention, delivery, channel) changes in
`scripts/` and restarts nothing. Put things on the correct side of that line
deliberately.

### Sections, in order

The order is the manual SOP's, which is itself ordered by what you would act on
first.

| # | Section | Source | Note |
|---|---|---|---|
| 1 | **Flat check** | position store + the `unmanaged` shape | **The headline.** Anything held, unwatchable or still working at 16:45 leads the digest and everything else is detail. |
| 2 | **G1 — NLV and day P&L** | `gate.decision` rows | first / last / min / max across the day, plus the count of rows behind it. **"no series (no candidates today)"** when there are none — never `0.0`. |
| 3 | **Rails** | `/status` sample + the chain | kill switch, breaker (live object, never a `daily_loss.breach` row), halt grade, the three-state watchdog row, blackout runway in days. |
| 4 | **Execution** | `tools.desk` | including the spent-exit row, `mutation_error` and the broker's `reject_reason` — the three things the 2026-09-14 fix put on that page. |
| 5 | **Scoring preview** | `tools.mark --dry-run --from-feed` | **skipped with a loud note on a half-day**, with the re-render command (`--date … --settlement …`) printed in its place. |
| 6 | **Evaluability** | `tools.evaluability` | |
| 7 | **Journal sweep** | the cheatsheet's grep set | `UNWATCHABLE POSITION`, `STALE ACCOUNT CACHE`, `REST confirm failed`, `residual-order recheck failed`, `BOOK DISAGREEMENT`. |
| 8 | **Backup** | newest file in `/path/to/backups` | name, age, and whether tonight's verify passed. |
| 9 | **Regime** | `tools.regime` | **last trading day of the week only** — computed from the calendar ("is the next trading day in a different ISO week?"), never `[[ $(date +%u) == 5 ]]`, so a Thursday-close week still gets its weekly read. |

**The report always names its own subject**, on the snapshot script's rule: the
profile, the config filename and sha, the database path, the trading date and the
running version, on the first line. A report that does not say which world it
describes is the failure `argus-snapshot.sh` documents at length.

**Amended 2026-09-18 (v0.4.4): a blackout day is named.** Live day 1 (2026-09-16, FOMC)
produced the digest's `no series (no candidates evaluated against the account)`. That
was true, and it could not be told apart from a broken signal path. The report
date's blackout is now read **off the chain** (`read_blackout_day`), from the
v0.4.4 session-start row `session.event_blackout` or from `signal.none` refusals with
reason `event_blackout`. It never comes from `/status`, which answers for the moment
it was polled rather than for a re-rendered date. It never comes from
`blackout_dates.txt` either, because the generator writes a forward horizon and a
regenerated file drops past dates.

- The subject block gains an `EVENT BLACKOUT` line.
- G1's "no series" says the outcome was designed.
- The digest's `entries` and `G1` lines say so, and the digest JSON gains
  `blackout_day`.
- **The next session's blackout is the one fact taken from `/status`**, because it
  is about the future: a `next session` line in RAILS, and a `next` heads-up line in
  the digest when tomorrow is blacked out.
- **The one blackout alarm:** an entry attempt on a blackout day, which means the
  strategy's rail did not hold.

→ [`/status` says when a day is blacked out](2026-09-18-status-says-when-a-day-is-blacked-out.md)

### The digest

~30–40 lines, as a Slack code block: subject line, flat/not-flat, G1 pair, entries
/ fills / realized, rails state, section failures, snapshot age, and the path of
the full transcript on the droplet. Its job is to be readable on a phone and to
make you go look when something is off — not to reproduce the report.

---

## Failure semantics

Three rules, in priority order.

**A failed section ships the report anyway.** Its stderr is pasted into the
transcript under the section heading and the section is named in the digest's
`failures` list. A broken `desk` must not cost you the flat check — the sections
are independent reads and the whole value of §1 is that it is above all the others.

**A failed JOB pages.** If the report cannot be produced or delivered at all, the
unit exits non-zero and posts the `pull-check`-style rotating-light message. A
missing report must never be indistinguishable from a quiet day.

**Absence is the signal, and it is watched by you, not by a machine.** The daily
digest is its own heartbeat. This is the seventeen-days-of-silence lesson written
into `argus-pull-check.sh` — a mechanism that fails by going quiet needs a watcher
that lives somewhere else. Here that watcher is the operator, because a digest that
does not arrive on a weekday evening is loud to a human reading it daily and has no
cheaper mechanical equivalent. **If this job ever stops being read every day, it
needs a `pull-check` arm of its own.** Say so in the README rather than pretending
the property is structural.

---

## Delivery

**Slack incoming webhook, its own channel, its own variable.**
`ARGUS_REPORT_SLACK_WEBHOOK_URL` -> `#argus-eod`, added to `.env` and
`.env.example`. Deliberately **not** the alert webhook: a daily digest in the
channel where Tier 1 pages land trains the reader to skim it. A missing report
webhook degrades to journal + file and never fails the job — the same rule the app
applies to `ARGUS_SLACK_WEBHOOK_URL`, for the same reason.

**Why not email.** DigitalOcean blocks outbound port 25 on droplets and does not
unblock it, so email means a relay (Mailgun / Resend / SES) on 587: a third-party
account, an API credential in `.env`, and deliverability to maintain. Slack is
already wired, already tested on this host, and already where the alerts go. If the
mailbox archive turns out to matter more than the phone push, this is a change of
one function in `argus-eod.sh` and nothing else — the generator does not care.

**Why the full transcript is not in Slack, for now.** An incoming webhook cannot
upload a file, and a long code block truncates. Posting the full text needs a Slack
**bot token** with `files:write` — a real but small addition, and one that can be
made later without touching `tools.eod`, because the transcript is already a file
on disk by then.

---

## The archive, and what stops living in git

**Transcripts:** `/path/to/backups/reports/argus-eod-<YYYY-MM-DD>.{txt,json}`.
Under `/path/to/backups` deliberately — off the Docker volume, so `down -v`
cannot take them, and **inside the path the NAS key is already restricted to**
(`command="rrsync -ro /path/to/backups"`), so no `authorized_keys` change is
needed. Retention 90 days: these are kilobytes, unlike the 14 snapshot files.

**The NAS side is in this repo, and it is DONE** (corrected 2026-09-18 — this
section originally called it "outside this repo" and pointed at
`pull-snapshots.example.sh`, which is only a generic example). The script actually
installed on the NAS is **`scripts/argus-pull.sh`**, run every 6 h by
`argus-pull-daemon.sh`; runbook §13 copies all three onto the NAS. It globbed
`/argus-*.db` only. It now has a **second, independent rsync arm** for
`/reports/argus-eod-*` → `/path/to/nas/argus-pull/reports/`, after the snapshot
arm and logged on its own line, so a reports failure can never cost a snapshot pull.
It is not `--ignore-existing`, because a re-render overwrites a report in place.
`tests/test_deploy_wiring.py` pins two things: the arm exists, and the report
directory stays inside the key's rrsync root, with the path given relative to that
root. `argus-eod.sh` now chmods its files `0644` explicitly, as
`argus-snapshot.sh` does, rather than trusting the container's umask.
**Deployed by copying `argus-pull.sh` onto the NAS.** The daemon runs the file again
every cycle, so no restart is needed (runbook §13).

**What still watches it, stated exactly.** `argus-pull-check.sh` sees NAS *logins*
by key fingerprint, and both arms log in with the same key. So it catches the NAS
going dark (both arms dead), but it **cannot see the reports arm failing while the
snapshot arm succeeds**. That failure shows only in the NAS's own `pull.log`
(`reports pull FAILED`). Accepted for now, because the transcript also exists on
the droplet for 90 days and in Slack. The fix, if it is ever needed, is a **second
key** restricted to `rrsync -ro /path/to/backups/reports`, so the droplet check
can tell the two arms' logins apart. That costs a new `authorized_keys` line and a
key on the NAS, which is why it was not built with this.

**`docs/reconciliation-live.txt` stops growing, and nothing replaces it in git.**
The argument is not tidiness:

- **the report is derived data.** Every section re-renders from the audit chain
  with `--date`, and because the transcript records the SPX close it used, even the
  scoring re-renders exactly. Committing a regenerable artifact is committing a
  cache;
- **it grows without bound** in a repo whose own convention section polices size
  and re-audits `CLAUDE.md` at 300 lines;
- **it is real-money P&L in a tree that gets pushed**;
- **the droplet would need a git WRITE credential** to do it automatically — new
  standing attack surface on the trading host, for an artifact the NAS already
  collects.

**What stays in git is what a human wrote:** the `docs/go-live-log.txt` entry, made
when something actually happened. That is the artifact worth reading in six months,
and it is worth *more* when it is not buried in ninety identical clean days.

---

## Rails — things this job must never do

Each of these is one convenience away from being violated by a future edit.

- **Never run a write-mode `tools.mark`.** It takes the writer lock, which needs
  the service stopped, which an unattended timer must never do. Weekly marking
  stays a human act on a weekend, with the book flat.
- **Never stop, restart or signal `argus.service`.** The report is a reader. It
  has no business touching the process it reads about.
- **Never take the writer lock at all** — every read is `mode=ro`.
- **Never parse another program's prose** to make a decision or a number.
- **Never default the profile.** No `${ARGUS_ENV:-sandbox}`, no literal `sandbox`,
  no third parser of `host.env` inside the container. The unit supplies it; compose
  refuses without it.
- **Never let the report's failure reach trading.** No `Requires=argus.service`,
  no shared state, no lock, nothing the trading process awaits.
- **`GET /report` on the control surface was considered and rejected.** It would
  put report rendering inside the trading process, on the stdlib `asyncio` listener
  whose minimalism is a decision of record, and would make an EOD bug a trading-process
  bug. The report reads the same database from a separate container instead.

---

## What the build has to confirm, and what it has to update

**Confirm first (each changes a section's implementation, none changes the shape):**

1. the exact payload keys `gate.decision` writes for `account_nlv` / `daily_pnl`,
   and whether any other Tier 1 category carries a `RiskSnapshot` on a day with no
   candidates;
2. where the **flat check** is best sourced — the `/status` `unmanaged` section,
   the position store directly, or both cross-checked (both is probably right, and
   a disagreement between them is itself a finding);
3. that a `docker compose run` container can reach the Tailscale-bound `/status`
   (it should: the service is `network_mode: host` and `run` inherits it) and that
   `ARGUS_KILL_TOKEN` is readable in that container from `.env`.

**Update, or the job does not exist on the box** (all but the last two are DONE):

- ~~`scripts/argus-deploy.sh` — the ancillary-unit case~~ **DONE**;
- ~~`tests/test_deploy_wiring.py` — both units in the pinned tuple~~ **DONE**, plus
  three more: the unit supplies the profile and the script refuses without it; the
  script names no profile and no database by literal; and the job can never take the
  writer lock (`--dry-run` present, no `systemctl stop` anywhere);
- ~~`.env.example` — `ARGUS_REPORT_SLACK_WEBHOOK_URL`~~ **DONE**;
- ~~`README.md` §4 — install instructions beside the snapshot's~~ **DONE**;
- **OWED: `README.md` §3** — the daily rhythm table and the after-the-close block
  become *"read the digest"*, with the manual commands kept as the by-hand path.
  Deliberately deferred until the week of running both has happened;
- ~~the second rsync arm for `reports/`~~ **DONE 2026-09-18** in `scripts/argus-pull.sh`
  (the NAS's real script; it was wrongly recorded here as outside the repo) —
  **deploying it is copying that file to the NAS**.

**Also owed, and not part of this job:** `README.md`'s daily-rhythm table still
describes the shadow era ("order recording isn't built yet", "the restart is cheap
in shadow"). It should be corrected in the same pass that rewrites the block above
it, or it will be the one stale paragraph beside a freshly accurate one.

---

## Rollout

**Build it, run it by hand for a week, then enable the timer.** The by-hand
invocation is the unit's own command line with the profile on `sudo`'s:

```bash
PROFILE="$(sudo sed -n 's/^ARGUS_ENV=//p' /etc/argus/host.env)"
sudo ARGUS_ENV="$PROFILE" /opt/argus/scripts/argus-eod.sh
```

Run it beside the manual SOP for a week and **diff what it says against what you
would have pasted**. The report is going to become the thing you trust instead of
looking, so the week where you still do both is the only chance to catch a section
that renders plausibly and wrongly — which, per 2026-09-14, is the failure mode
this class of instrument actually has.

**A deferred option, deliberately not built now:** a second timer sampling
`/status` every 15 minutes through the session into a JSONL file. It would give an
NLV path on blackout and no-candidate days, where `gate.decision` writes nothing.
It is not needed while the G1 question is *"does it move on a day we traded?"*, and
it is a second unit, a second token read and a second thing to watch. Revisit if a
no-candidate day ever needs a P&L path.
