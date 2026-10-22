# Build

**Use when:** a design in `docs/designs/` has been signed off (all, or named parts of,
its "Decisions for sign-off"). This is the "build" form of
`docs/dev-code-prompt-template.md`.

**Produces:** code + tests + config on the working branch, and the documentation that
records it:

- the decision record in `docs/decisions/` (new, or the investigation record updated),
  marked **BUILT, NOT YET DEPLOYED**;
- a "Build notes (date)" section appended to the design;
- one current-state sentence in `CLAUDE.md`;
- the `docs/decisions/README.md` row.

**Mode:** BUILD. Implements the approved decisions *only*. Never tags, deploys or
restarts.

**Next step:** template `04-code-review`, then `08-release`.

| Placeholder | Fill with |
|---|---|
| `{{DESIGN}}` | Path to the approved design |
| `{{APPROVED}}` | What was approved and any changes made at sign-off, e.g. "D1–D8 as written; D2 decided k = 10" |
| `{{NOT_APPROVED}}` | Parts deferred or rejected (or "none") |
| `{{DATE}}` | Today |

---

## The prompt

````text
Before beginning, read docs/templates/00-standing-preamble.md in full and follow it.

Then read {{DESIGN}} in full, every document it links to, and the decision records
for every component it touches.

Sign-off: {{APPROVED}}
Not approved / deferred: {{NOT_APPROVED}}

Now: BUILD the approved design. Implement exactly what was approved — nothing that was
deferred, nothing the design did not describe. If the code shows the design is wrong or
incomplete somewhere, stop at that point and report it with a proposed amendment rather
than improvising; a small, clearly-safe correction may be made and must be recorded in
the build notes as a deviation.

The standard: complete, high quality, performant, and it must introduce NO defects.
Trading-system best practice first, general architecture second.

How to work:
1. Establish a baseline: run the full suite
   (./.venv/bin/python -m pytest) and record the count before any change.
2. Write the tests the design lists FIRST, and see each one fail for the reason it
   should. A test must drive the path production drives (through run() / check_once()
   / the real seam), not a helper production never calls at that moment.
3. Implement. Match the surrounding code's idiom, naming, and comment density. Every
   new config key goes into all three profiles (argus.dev.toml, argus.sandbox.toml,
   argus.live.toml) with the values the design gave, and tests/test_config_profiles.py
   must pass (identical key sets; any intended divergence declared on its allowlist).
4. Mutation check the load-bearing lines. For each line the design names (plus any you
   judge load-bearing), revert or invert it, run the relevant tests, confirm at least
   one fails, restore it. Report "N mutations, N caught" and list any survivor with
   either a new test or the reason it is equivalent.
5. Run the full suite again. Report the new count, and any skip with its reason. A
   failure is not "pre-existing" unless you show it fails on the base commit too.
6. Re-read your own diff adversarially, as a reviewer who wants to find the defect:
   NaN and None paths, restart and rehydration, half-day close times, a broker that
   does not answer, a Tier 1 write with a non-finite value, an alert missing a
   top-level candidate_id, anything that reads broker Order.direction instead of
   parse_external_identifier, any symbol-keyed count, any float compared without
   PRICE_EPSILON. Fix what you find before writing the docs.

Documentation, in the same change:
- The decision record: create docs/decisions/{{DATE}}-<slug>.md (or update the
  investigation record the design came from) with a status block —
  "BUILT {{DATE}}, NOT YET DEPLOYED", release class (image-only / config-only /
  coupled, and why), new REQUIRED keys, suite count before → after, mutations caught —
  then the decision itself and the rules worth carrying, pointing at the design for
  the narrative. Add it to docs/decisions/README.md.
- Append "## N. Build notes ({{DATE}})" to {{DESIGN}}: what the build changed or found
  against the design, numbered, including every deviation. Update the design's status
  block to BUILT.
- CLAUDE.md: ONE current-state sentence (plus pointers) in "Current phase", saying
  BUILT, NOT YET DEPLOYED and whether the config deploys with the image. No narrative.
- If the change moves a component, flow, state machine, route, volume or timer, add a
  line to the build notes saying docs/architecture/ is owed an update (template 05).

Commit in logical steps with messages that say what and why, and push to the branch you
were given. Do not tag, do not deploy, do not open a pull request unless asked.

Finish with a short report: what was built, suite before → after, mutations caught,
deviations from the design, anything left for the review.
````

---

## Notes for the operator (not part of the prompt)

- **Partial sign-off** is common (e.g. "B, C and D now; A next week"). Put the split in
  `{{APPROVED}}` / `{{NOT_APPROVED}}` — the prompt forbids building the rest.
- **When the build finds the design wrong**, the prompt stops the session rather than
  letting it improvise. Answer with a revision to the design (template `02` notes) or
  an explicit amendment in your reply.
- **The adversarial re-read in step 6 is not the review.** Run `04-code-review` in a
  fresh session anyway; a reviewer that did not write the code finds different things.
