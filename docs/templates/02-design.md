# Design

**Use when:** an investigation record, a business spec, or an open decision needs a
solution designed *before* any code. This is the successor to
`docs/dev-code-prompt-template.md` in its "design" form; the "build" form is template
`03`.

**Produces:** `docs/designs/YYYY-MM-DD-<slug>.md`. Model it on
`docs/designs/2026-09-25-deferring-a-far-otm-flatten.md` (status block, revision
history, recommendation in one paragraph, per-part sections, "what this changes nothing
about", constant audit, considered-and-not-done, release plan, decisions for sign-off).

**Mode:** DESIGN. No `src/`, `tests/` or `argus.*.toml` change. Read-only scratch
analysis against a NAS snapshot is allowed and encouraged; its scripts go in the session
scratchpad, and the design specifies the committed tool that replaces them.

**Next step:** Richard signs off (or revises) §"Decisions for sign-off"; then template
`03-build`.

| Placeholder | Fill with |
|---|---|
| `{{SOURCE_DOCS}}` | The record(s) being designed for — an investigation in `docs/decisions/`, a spec in `docs/specs/`, or both |
| `{{EXTRA_READING}}` | Any other decisions/research the design must honour (optional) |
| `{{SNAPSHOT}}` | NAS snapshot(s) to read, e.g. `dev/nas/argus-20260925T163001.db` (optional) |
| `{{SCOPE_NOTES}}` | Anything you already know you want or reject (optional) |
| `{{DATE}}` / `{{SLUG}}` | Date and file slug |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read, in full:
- {{SOURCE_DOCS}}
- every decision record those documents link to, and any other record in
  docs/decisions/README.md that covers a component this design will touch
- {{EXTRA_READING}}

Evidence you may read (read-only, mode=ro): {{SNAPSHOT}}

Owner's notes on scope: {{SCOPE_NOTES}}

Now: design the solution to {{SOURCE_DOCS}}.

The standard:
- It must completely resolve every finding in the source documents. If you conclude a
  finding should NOT be fixed (closed by measurement, not worth its risk, belongs to
  another release), that is a decision for sign-off with its reasoning and its reopen
  triggers — never a silent omission.
- It must introduce NO defects. For every new path, state its failure direction (input
  missing, stale, NaN, unreadable, broker silent, restart mid-way, half-day, the 15:30
  flatten, a position whose exit is spent) and show it fails closed. For an exit path,
  "closed" means keep watching and keep the flatten.
- High quality and performant: nothing new may block the event loop, and every await
  inside the watchdog's check takes its bound from the thing awaited, never from a loop
  cadence.
- Trading-system best practice first, general architecture best practice second
  (docs/architecture/general-best-practices.md). Where they conflict, say so and say
  which you chose.
- Numbers come from data or from an existing key. Audit every constant you introduce:
  derived from what, or stated as a policy choice for Richard. No price levels, points
  or percentages where a dimensionless or measured quantity is the real quantity.
- Mode: DESIGN. Do not change src/, tests/ or any argus.*.toml.

Structure the document as:

0. Title that states the outcome, not the component. Status block ("DESIGN, awaiting
   sign-off", date, links to the source docs), the standing rules restated in bullets,
   and "line numbers are against main at <sha>; the named sites are the anchor".
1. Recommendation in one paragraph.
2. What happened / what is true today — the evidence, with audit seq numbers, snapshot
   file and window, and code anchors.
3. Findings table — every finding from the sources plus any new ones you found reading
   the evidence, each mapped to the part of the design that closes it.
4..N. One section per part: the mechanism; where it lives (module::name) and why
   there; failure handling; records (Tier 1 vs Tier 2, and why), pages (per-position
   alerts carry candidate_id at the TOP level of the detail), and /status; config
   (every new key: REQUIRED or not, value in each of the three profiles, and whether it
   is cert-only and fenced in config.py); tests, including an explicit list of the
   load-bearing lines whose mutation must fail a test, and any AST test that pins a
   structural rule.
- What this changes nothing about — the rails, paths and decisions deliberately
  untouched.
- Constant audit — every number in the design and where it comes from.
- Considered and not done — each alternative and why not.
- Release plan — image-only, config-only, or coupled; the release class the deploy
  script's classifier will report; migration or not; rollback, and "never with a
  position open" where that applies; what to read on /status after deploy.
- Environment assumptions — preamble §4, answered.
- Decisions for sign-off — numbered D1..Dn, each a single decision with your
  recommendation. Anything that is the owner's policy call (a budget, a multiple, a
  threshold) is listed here, not decided in the body.

Save the output as an .md formatted file at docs/designs/{{DATE}}-{{SLUG}}.md, add a
row to docs/designs/README.md, commit, and push to the branch you were given. Do not
update CLAUDE.md for a design that is not yet approved.
````

---

## Notes for the operator (not part of the prompt)

- **Revisions.** When you send it back, keep the same file and add a "Revision N"
  bullet to its revision history saying what changed and why (the 2026-09-25 design
  shows the pattern). Prompt: *"Read docs/designs/<file> and my review below. Produce
  revision N: …"*.
- **Tightening the scope.** Add to `{{SCOPE_NOTES}}` lines like *"one release, not
  several"* or *"nothing in this design may add REST load"* — constraints stated up
  front produce better designs than constraints applied at review.
- **Big designs.** For a multi-part design, ask for a per-part risk review (see
  §12.1 of the 2026-09-25 design) before sign-off: it is where the "ship it on vs.
  record-only" call gets made.
