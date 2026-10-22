# Standing preamble — read by every template

Every prompt in `docs/templates/` opens by telling the session to read this file. It
holds the rules that apply **regardless of task**, in one place, so the templates
cannot drift apart on them. If a rule here changes, it changes for every
template at once — that is the point. Keep it short: anything task-specific belongs
in the template that needs it.

This file is written *to the model*. It is not a prompt on its own.

---

## 1. Read before acting

1. **`CLAUDE.md`, in full.** It carries the non-negotiables (kill switch, audit chain,
   single writer, daily loss breaker, size rails, broker as the only source of truth,
   single-legging, no broker-side stops, COA timing from config, sandbox before live,
   durable audit writes) and four standing landmines. They are the contract every task
   lives inside, whatever the task file says.
2. **The decision records that cover the components you will touch** —
   `docs/decisions/README.md` is the index. They are **normative, not history**.
   "Do not relitigate" means what it says: if your work contradicts one, stop and say
   so rather than routing around it.
3. **`docs/architecture/general-best-practices.md`** — trading-system practice is the
   FIRST priority, general architecture practice the SECOND. When they disagree, the
   trading-system rule wins, and you say that you chose it.
4. **The source, not the prose.** Where a document states a value (a config key, a
   count, a threshold, a line number), check it against `src/` and the
   `argus.*.toml` profiles before relying on it. Prose lags; CLAUDE.md says so about
   itself. Cite code by NAME (`watchdog.py::PositionWatchdog.check_once`), never by
   line number alone — line numbers drift within days.

## 2. The databases

**Never write to `argus-live.db` or `argus.db`. Ever.** `argus-live.db` is real money
and real fills; `argus.db` is the finished cert chain and the signal record.

- Any read for analysis is read-only:
  `sqlite3.connect("file:<path>?mode=ro", uri=True)`.
- Prefer a NAS snapshot under `dev/nas/` over anything on the droplet, and name the
  file you read in your output.
- Any dev or test run uses `argus.dev.toml` (`ARGUS_ENV=dev`, or `--config
  argus.dev.toml` for `argus.tools.*`). Never a bare `python -m argus`, never a bare
  `argus.tools.*`, never the sandbox or live profile.
- **Not every tool is a reader.** `argus.tools.*` take `db_path` from `--config`, and
  some write (`tools.mark` does unless `--dry-run`). To run one over a snapshot, point
  a scratch config derived from `argus.dev.toml` at a scratch COPY of the snapshot.

## 3. What a session may and may not do

- **Stay inside the mode the template names** (report-only, design, build). A design
  session changes no `src/`, no `tests/`, no `argus.*.toml`. A report-only session
  changes nothing but its report. "While I was in there" fixes are out of scope in
  every mode — list them instead.
- **Never tag, deploy, restart, or touch `/etc/argus/host.env`.** Those are Richard's
  actions, done by hand, from the runbook.
- **Never change a live-affecting config value on your own judgement.** A value in
  `argus.live.toml` moves only on a decision record Richard has signed off.
- **A new REQUIRED config key goes into all three profiles** (`argus.dev.toml`,
  `argus.sandbox.toml`, `argus.live.toml`) and makes the release a COUPLED image +
  config deploy. Say so wherever the release class is stated.
- **Ask, don't assume, on anything that is the owner's call**: a risk budget, a size,
  a policy constant, a reversal threshold. Offer a recommendation with its reasoning;
  mark it as awaiting sign-off.

## 4. Environment-assumption checkpoint

At every design, decision record and spec, answer explicitly (retrospective
[2026-09-01, I6](../retrospectives/2026-09-01-dedicated-account-oversight.md)):

> What am I assuming about the deployment environment, the account, the operator, the
> broker, and the other systems that share infrastructure with this one — and which of
> those has a human confirmed?

Then the mirror question: **what does cert/sandbox make true for free that live will
not?** (Day P&L is structurally zero in cert; fills there are not realistic; the
account was never shared.) Each answer is a candidate live gate.

## 5. How to write

- **Evidence over assertion.** Every load-bearing claim carries its source: a code
  anchor, a test name, an audit `seq`, a snapshot file and window, or "unmeasured".
- **Mark what you could not verify** as `> **Unverified:**` or `> **Assumption:**`,
  inline, rather than stating it as fact.
- **Say what it does NOT change and what it does NOT license.** The commonest failure
  in this repo is a right number read as permission.
- **Name the failure direction.** For every new path: what happens when its input is
  missing, stale, NaN, unreadable, or the broker does not answer? The answer must fail
  closed, and for an exit, "closed" means *keep watching and keep the flatten* — never
  "stop managing the position".
- **Dates are absolute** (`2026-09-26`, ET), never "today" or "yesterday".
- **Match the repo's voice**: short declarative sentences, bold for the rule, a
  pointer (`→ record (private record, not published)`) instead of repeated narrative.

## 6. Bookkeeping every template ends with

- A new `docs/decisions/` file is added to `docs/decisions/README.md` so it is not
  orphaned; a new research note to `docs/research/README.md`.
- CLAUDE.md gets **one current-state sentence plus a pointer**, never the narrative
  (it is ~1,800 lines against its own ~300-line re-audit threshold; do not make that
  worse).
- If the change moves a container, component, flow, or state machine, say that
  `docs/architecture/` is owed an update (template `05`) — do not silently skip it.
- Commit on the branch you were given, with a message that says what and why. Do not
  open a pull request unless asked.
