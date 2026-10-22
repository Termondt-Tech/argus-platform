# Code review

**Use when:** a build is done and before it is released — ideally in a **fresh
session** that did not write the code. Also usable on any branch, PR, or commit range.

**Produces:** a findings table, written where this repo already keeps them — a
`## Review (YYYY-MM-DD)` section in the build's decision record (the shape of
"Review fixes (2026-09-25)" in
`docs/decisions/2026-09-25-deferring-a-far-otm-flatten.md`). If the change has no
decision record (a tool, a script), use `docs/reviews/YYYY-MM-DD-<slug>.md`, creating
the directory on first use.

**Mode:** two phases, chosen by the placeholder.

- `REPORT` — findings only, no code change. The default.
- `FIX` — the same review, then fix every finding marked blocking or should-fix, each
  pinned by a test that fails when the fix is reverted.

**Next step:** `08-release`.

| Placeholder | Fill with |
|---|---|
| `{{TARGET}}` | What to review: `main..HEAD`, a branch, a PR link, or a commit range |
| `{{DESIGN_AND_RECORD}}` | The design and decision record the build claims to implement |
| `{{MODE}}` | `REPORT` or `FIX` |
| `{{FOCUS}}` | Optional: areas you are worried about, or "none" |
| `{{DATE}}` | Today |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read {{DESIGN_AND_RECORD}} in full, and the decision records for every component
the change touches.

Review target: {{TARGET}}
Mode: {{MODE}}
Owner's focus: {{FOCUS}}

Task: review the change for DEFECTS — correctness, safety, and fidelity to the approved
design — as a reviewer whose job is to find what the author missed. This is not a style
pass; a style note is a nit and is labelled as one.

Read the whole diff, then the code around every changed site (callers and callees one
level out), then the tests. For each finding, reproduce it where you can: a failing
test, a concrete input sequence, or an audit trail it would produce. A finding you
cannot turn into a concrete failure scenario is "speculative" and labelled so.

Check, at minimum:

A. Fidelity. Does the build implement every approved decision, and nothing that was
   deferred? Is every deviation recorded in the design's build notes?

B. The non-negotiables and landmines in CLAUDE.md. Can anything new reach the
   submission path outside pipeline.trades_for_real? Emit a per-leg order? Place a
   broker-side stop? Hardcode COA timing? Write before the audit record? Raise size
   past a rail, or break max_concurrent_positions × max_loss_per_position ≤
   daily_max_loss? Trip the kill switch from a per-day source?

C. The defect classes this codebase has already paid for (each has a decision record):
   - an absence, an empty answer or a partial answer read as a verdict;
   - a network await in a halt region, or a retry bounded by COUNT where RATE is the
     right bound (or the reverse), or a retry wrapping the verdict instead of the
     question;
   - an invariant built for the entry path reused on the exit path (or the reverse)
     without re-deriving its precondition;
   - a copy between two dataclasses whose target has None defaults (silent under-copy);
   - a reader that filters on ONE audit category and so makes a closed-world claim;
   - a float compared without PRICE_EPSILON; a refusal written so that NaN passes;
   - a Tier 1 write that can carry a non-finite value (it raises ValueError, which
     SignalLoop.run swallows);
   - broker Order.direction used where parse_external_identifier is the authority;
   - a symbol-keyed count of spreads (spreads chain and net to zero);
   - a per-position alert with candidate_id only nested, not at the top level;
   - state that does not survive a restart, or rehydrates a stronger state than it
     left; a literal clock time that is wrong on a half-day;
   - a test that passes by calling the code at a moment production never does;
   - a new REQUIRED key missing from one of the three profiles.

D. Failure direction. For every new path: input missing / stale / NaN / unreadable,
   broker silent, restart mid-way, the 15:30 flatten, a spent exit. Does it fail
   closed, and for an exit, keep watching?

E. Performance. Anything new on the event loop that can block; any await inside the
   watchdog's check bounded by a loop cadence instead of by the thing awaited; any
   unbounded read of the audit log.

F. Tests. Does each load-bearing line have a test that fails when it is mutated? Spot
   check at least five by mutating them yourself (and restore them).

Write the findings as a table:
| # | Severity | Confidence | Finding | Failure scenario | Where (module::name) | Proposed fix |
Severity: blocking / should-fix / nit / pre-existing (not introduced by this change).
Confidence: confirmed / suspected / speculative. Rank most severe first. Include
"checked, no issue" rows for anything you examined closely and cleared, so the next
pass does not re-audit it. Include open questions separately, numbered Q1..Qn.

Where: add a "## Review ({{DATE}})" section to the decision record named in
{{DESIGN_AND_RECORD}} (or docs/reviews/{{DATE}}-<slug>.md if there is none).

If Mode is REPORT: change nothing else. Commit the review and push.

If Mode is FIX: fix every blocking and should-fix finding, each in the safe direction,
each pinned by a test that fails when the fix is reverted (report "N mutations, N
caught"). Leave nits unless trivially correct and in a file you are already changing.
Do not fix pre-existing findings — list them for a separate task. Run the full suite.
Turn the table into "Review fixes ({{DATE}})" with a Fix column, update the decision
record's status block and the design's build notes, commit, and push.

Do not open a pull request unless asked. Finish with a one-paragraph verdict:
releasable as-is, releasable after the listed fixes, or not releasable and why.
````

---

## Notes for the operator (not part of the prompt)

- **Run REPORT first on anything that touches the exit path, the gate, or the kill
  switch**, and read the table before authorising FIX. On a small change, FIX in one
  go is fine.
- The built-in `/code-review` skill is a useful fast first pass; this template is the
  Argus-specific pass that knows the defect classes and the rails.
- The defect-class list in C is the living part. When a new class is paid for, add it
  here and to `docs/tasks/argus_proactive_defect_class_review_prompt_template.md`.
