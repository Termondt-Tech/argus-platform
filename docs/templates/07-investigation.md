# Investigation (a session, an incident, or a surprise)

**Use when:** something happened that needs explaining before anything is designed — a
live session worth a pass, a page that fired, a P&L that does not add up, a `/status`
reading that looks wrong. This is the step that has started most of this repo's
decision records (the "live-fire day" passes, 2026-09-24's flatten).

**Produces:** `docs/decisions/YYYY-MM-DD-<slug>.md` in the shape of
`docs/decisions/2026-09-24-a-flatten-that-crossed-below-its-own-ladder.md`: a status
callout (**OPEN — N FINDINGS, NOTHING BUILT, NOTHING DECIDED**), what happened as a
timed table with sources, one section per finding, "what is still to read" with the
exact query, and any data point for an open decision. Plus a row in
`docs/decisions/README.md` under open questions or closed investigations.

**Mode:** REPORT-ONLY. Read-only against a NAS snapshot and any exports you supply. No
`src/`, `tests/` or config change, and no proposed fix beyond a sentence per finding on
its likely shape and whether it needs a design.

**Next step:** template `02-design` on the findings that need one.

| Placeholder | Fill with |
|---|---|
| `{{WHAT_HAPPENED}}` | Your account: date, times (say the timezone), the position(s) by `candidate_id`, what you saw |
| `{{EVIDENCE}}` | Snapshot path, and any exports: broker Activity CSV, Slack paste, `/status` capture (paths in the repo or pasted) |
| `{{QUESTIONS}}` | What you want answered, if you already know |
| `{{DATE}}` / `{{SLUG}}` | Date of the event and file slug |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read the decision records for every component involved (the index is
docs/decisions/README.md — for an exit, at least the watchdog, the walk and the
flatten records; for an entry, the gate, the loss budget and the entry path), and
docs/decisions/README.md's "Open questions" and "Open decisions".

What happened, in the owner's words:
{{WHAT_HAPPENED}}

Evidence: {{EVIDENCE}}
Questions: {{QUESTIONS}}

Task: find out what happened and why, from the audit trail, and record it. Mode:
REPORT-ONLY — change nothing but the record you write.

Method:
1. Rebuild the timeline from the audit log (read-only, mode=ro, the snapshot named
   above), joined to the external evidence. Every row in the timeline carries its
   source (audit seq and category, export row, Slack page, /status field). Convert
   every time to ET and say which sources were in another zone.
2. Reuse the existing readers before writing new SQL: argus.tools.desk,
   tools.evaluability, tools.mark (ONLY with --dry-run — without it, it writes).
   These tools read db_path from their --config, so run them against a scratch COPY
   of the snapshot, through a scratch config derived from argus.dev.toml whose
   db_path names that copy — never the snapshot itself, never a production file.
   Remember a reader that filters on one audit category makes a closed-world claim;
   check the neighbouring categories (desk.MARK_CATEGORIES is the example).
3. For each anomaly, find the code path that produced it (module::name) and state the
   mechanism. Separate what the record PROVES from what you infer, and label the
   inference.
4. Classify every finding:
   - defect (the code did something other than its decision record says);
   - design gap (the code did what was decided, and the decision did not cover this);
   - reporting defect (the trading was right, a surface described it wrong);
   - expected behaviour (with the record that says so);
   - data point for an open decision (which one — and it does not reopen it).
   For each, one sentence on the likely shape of the fix and whether it needs a
   design sign-off. Do not design it.
5. Check it against the classes already paid for (absence read as a verdict, an entry
   invariant reused on exit, a halt region with a network await, a silent under-copy,
   a closed-world category filter, float equality, a symbol-keyed count, broker
   direction trusted) and say if it is a new instance of one.
6. Money: what did it cost or save, measured against holding to settlement AND against
   what the decided behaviour would have done — and say plainly that one day's P&L is
   not evidence about the rule.
7. Anything you could not read: write it as "What is still to read" with the exact
   read-only query and what each answer would mean.

Write docs/decisions/{{DATE}}-{{SLUG}}.md:
- Title: what happened, as a sentence, with the date.
- Status callout: "OPEN — N FINDINGS, NOTHING BUILT, NOTHING DECIDED", who raised it,
  the evidence used, what has not been read yet, and which findings need a design.
- 1. What happened — prose plus the timed table. Whether anything needs a human NOW
  (and if so, say it first, before any analysis — e.g. an order still working at the
  broker; follow the manual-close SOP's first step: check the broker before sending
  anything).
- 2..N. One section per finding: the evidence, the mechanism, the classification, the
  likely shape.
- What is still to read.
- Data points for open decisions (if any).

Add the row to docs/decisions/README.md. Do not touch CLAUDE.md for an open
investigation unless a finding changes how the system must be operated today — then
one sentence, flagged OPEN. Commit and push to the branch you were given.

Finish with the three-line summary: what happened, what needs a design, what needs a
human.
````

---

## Notes for the operator (not part of the prompt)

- **A routine post-session pass** (no incident) is the same template with
  `{{WHAT_HAPPENED}} = "routine pass over the YYYY-MM-DD session"` and
  `{{QUESTIONS}}` naming the checks you care about (every exit's outcome, every page,
  the gate's denial mix, the loss budget's use). Nothing found is a valid result; say
  so in one line in the README index rather than writing a file.
- **Anything urgent comes first.** If a position may still be open or an order may
  still be working, handle that from the runbook before starting a session on why.
