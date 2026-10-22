# Research

**Use when:** a design question needs evidence — a measurement over Argus's own audit
log, an outside paper read against what Argus actually trades, or a review of one
against the other.

**Produces:** `docs/research/YYYY-MM-DD-<slug>.md`, its scripts in
`docs/research/YYYY-MM-DD-<slug>-queries/`, and a row in `docs/research/README.md`.
Model it on `docs/research/2026-09-25-two-slot-side-composition.md` (a measurement) or
`docs/research/2026-09-26-cross-document-exploration.md` (pre-registered tests plus a
binding forward pre-registration).

**Mode:** research only. **Nothing in `docs/research/` is a decision** — it licenses
no change to `src/` or any `argus.*.toml`. It moves the needle only when a decision
record cites it. All database reads are `mode=ro` against a NAS snapshot.

| Placeholder | Fill with |
|---|---|
| `{{KIND}}` | `measurement`, `paper`, or `review` (see `docs/research/README.md`) |
| `{{QUESTION}}` | The question, as sharply as you can put it, and the decision it would inform |
| `{{SNAPSHOTS}}` | NAS snapshot file(s) and the date window, e.g. `dev/nas/argus-20260925T163001.db, 2026-09-15 → 09-25` |
| `{{PAPERS}}` | Paper file(s) or citation(s), for `paper`/`review` (or "none") |
| `{{HYPOTHESES}}` | Optional: hypotheses you want tested, with the sign you expect |
| `{{DATE}}` / `{{SLUG}}` | Date and file slug |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read docs/research/README.md in full — its "Adding a paper / a measurement / a
review" rules are binding for this task — and the research notes and decision records
it lists that bear on the question below.

Kind: {{KIND}}
Question, and the decision it would inform: {{QUESTION}}
Data (read-only, mode=ro): {{SNAPSHOTS}}
Papers: {{PAPERS}}
Owner's hypotheses: {{HYPOTHESES}}

Task: answer the question with evidence, and say exactly how far the evidence reaches.

Rules:
- Read-only. Every database read is
  sqlite3.connect("file:<snapshot>?mode=ro", uri=True) against the snapshot named
  above — never argus-live.db or argus.db on the droplet, never a write.
- No change to src/, tests/, or any argus.*.toml. If you need a reusable tool, say
  what it should be; do not build it into src/argus/tools/ in this session.
- Pre-register before you test. Before running any test, write the statistic, the
  unit, the predicted sign and the correction into
  docs/research/{{DATE}}-{{SLUG}}-queries/PREREGISTRATION.md. A test added after
  seeing data is labelled discovery, not confirmation. Say honestly that an in-session
  pre-registration has no external timestamp.
- The unit is the trading DAY, never a pooled tick (ticks within a day are not
  independent). Resample days for intervals; use a permutation or sign-flip test on
  day-level statistics; correct for the number of tests (Holm or Bonferroni, stated).
  Split discovery and confirmation where the data allows (cert through 2026-09-14,
  live from 2026-09-15 is the natural split).
- Model realistic fills: never score a trade at mid. State how exits and marks are
  priced and how well that pricing matches recorded watchdog marks.
- Keep the conventions the code uses (annualization basis, clock, which record a value
  comes from) — import them rather than re-deriving them, and say where from.
- Respect the standing prohibitions: conclusions about the credit stop come only from
  realized-basis positions, and a number measured in cert cannot size a change on its
  own. If the evidence sits inside a prohibition, say so at the top.
- For a paper: say whether its structure, width, horizon and cost treatment match what
  Argus trades. If you cannot state the mapping, the paper is not ready to cite.

Structure the note as:
0. Title that states the finding. An italic summary: the question, whose, and the
   one-line answer. A "NOT A DECISION" callout. The snapshot(s), window, session count
   after filtering and why sessions were excluded.
1. The question, and the decision it informs.
2. The arithmetic / the mechanism (what should be true before looking).
3. Method — enough to re-run: data, filters, pricing, statistics.
4. Results — each with its interval and its p-value after correction; nulls reported
   as prominently as effects.
5. What a decision record would weigh.
6. Findings — numbered, each one line.
7. What this does NOT license — written before the results are quoted anywhere.
8. Forward pre-registration (if the question can be settled by future sessions): the
   qualifying-session rule, the scoring script as committed, the look schedule and the
   alpha spend, and what is forbidden (editing the script, peeking, adding tests).
9. Reproducing — the exact commands, from a clean checkout, against the named
   snapshot.

Commit the note, the queries directory (scripts, PREREGISTRATION.md, and any small
derived CSVs; never a database), and the docs/research/README.md row, and push to the
branch you were given. If there is a forward pre-registration, the commit must land
BEFORE the first qualifying session — say which session that is.
````

---

## Notes for the operator (not part of the prompt)

- **Sharpen `{{QUESTION}}`** until it names a decision: *"Would a second slot on the
  same side raise the day's tail?"* produces research; *"look at the two-slot data"*
  produces a tour.
- **A negative result is a result.** The 2026-09-26 exploration's headline was that
  none of five tests survived correction — and it still reshaped v0.5.2.
- **For a `review` of outside material**, add to `{{QUESTION}}`: *"tag every finding
  exactly once: already handled / owed / rejected, with the code anchor"* — that is the
  README's rule and the reason reviews exist.
